"""Transformer machines: consume recipe inputs and create recipe outputs in production cycles.

Cycle rules:

* A cycle starts only when the machine is operational, has a recipe, every input slot buffer
  holds the required quantity of the required product type, and every output buffer has room
  for the recipe outputs. Inputs are then reserved atomically (moved out of the input buffers
  into the cycle) and a production-cycle ID is created.
* The effective recipe, operating speed, time multiplier, quality score and quality model are
  snapshotted at cycle start; later configuration changes only affect the next cycle.
* On completion, reserved inputs are consumed (status ``consumed``) and exactly the
  recipe-defined outputs are created with new product IDs.
* Cancellation (``stop`` or ``fault`` command during a cycle) restores the reserved inputs to
  the front of their input buffers in their original order; nothing is consumed or produced.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from ..curves import QualityModel, SpeedModel, defect_probability, validate_speed
from ..errors import ConflictError, ValidationError
from ..models import (MAX_RECIPE_INPUTS, MAX_RECIPE_OUTPUTS, Product, ProductionCycle, ProductStatus,
                      Recipe, require_positive_int)
from ..scheduler import Priority
from .base import BLOCKED, IDLE, PROCESSING, UNCONFIGURED, Asset, parse_initial_state

IN_PORTS = tuple(f"in{i}" for i in range(MAX_RECIPE_INPUTS))
OUT_PORTS = tuple(f"out{i}" for i in range(MAX_RECIPE_OUTPUTS))


class Transformer(Asset):
    kind = "transformer"
    input_port_names = IN_PORTS
    output_port_names = OUT_PORTS

    def __init__(self, sim, asset_id, name, config, position=None) -> None:
        self.input_buffers: list[deque[str]] = [deque() for _ in IN_PORTS]
        self.output_buffers: list[deque[str]] = [deque() for _ in OUT_PORTS]
        self.active_cycle_id: str | None = None
        self._completion_event: int | None = None
        self._reservation_entries: dict[str, Any] = {}
        self.cycle_seq = 0
        self.stats_counters = {"cycles_completed": 0, "cycles_cancelled": 0, "inputs_consumed": 0,
                               "outputs_generated": 0, "defective_outputs": 0, "busy_time_s": 0.0}
        super().__init__(sim, asset_id, name, config, position)
        if self.state != "stopped" and self.recipe is None:
            self.set_state(UNCONFIGURED, "no recipe selected")

    @classmethod
    def parse_config(cls, raw: dict, sim) -> dict:
        recipe_id = raw.get("recipe_id")
        if recipe_id is not None and recipe_id not in sim.recipes:
            raise ValidationError(f"unknown recipe '{recipe_id}'")
        return {
            "recipe_id": recipe_id,
            "speed_pct": validate_speed(raw.get("speed_pct", 50.0)),
            "input_buffer_multiplier": require_positive_int(raw.get("input_buffer_multiplier", 2),
                                                            "input_buffer_multiplier"),
            "output_buffer_multiplier": require_positive_int(raw.get("output_buffer_multiplier", 2),
                                                             "output_buffer_multiplier"),
            "quality_model": QualityModel.from_dict(raw.get("quality_model")).to_dict(),
            "speed_model": SpeedModel.from_dict(raw.get("speed_model")).to_dict(),
            "initial_state": parse_initial_state(raw),
        }

    @property
    def recipe(self) -> Recipe | None:
        rid = self.config.get("recipe_id")
        return self.sim.recipes.get(rid) if rid else None

    def time_multiplier(self, speed_pct: float | None = None) -> float:
        return SpeedModel.from_dict(self.config["speed_model"]).multiplier(
            self.config["speed_pct"] if speed_pct is None else speed_pct)

    def quality_score(self, speed_pct: float | None = None) -> float:
        return QualityModel.from_dict(self.config["quality_model"]).quality(
            self.config["speed_pct"] if speed_pct is None else speed_pct)

    def conditions(self) -> dict:
        return {"config_version": self.config_version, "recipe_id": self.config.get("recipe_id"),
                "speed_pct": self.config.get("speed_pct"), "cycle_id": self.active_cycle_id}

    def effective_boundary(self, changed: set[str]) -> str:
        if self.active_cycle_id:
            return f"next cycle (active cycle {self.active_cycle_id} keeps its configuration snapshot)"
        return "next cycle"

    # ------------------------------------------------------------------ connections & config
    def validate_input_connection(self, port: str) -> None:
        super().validate_input_connection(port)
        r = self.recipe
        if r is not None and IN_PORTS.index(port) >= len(r.inputs):
            raise ValidationError(f"recipe '{r.id}' has only {len(r.inputs)} input slot(s); cannot use {port}")

    def validate_output_connection(self, port: str) -> None:
        super().validate_output_connection(port)
        r = self.recipe
        if r is not None and OUT_PORTS.index(port) >= len(r.outputs):
            raise ValidationError(f"recipe '{r.id}' has only {len(r.outputs)} output slot(s); cannot use {port}")

    def check_recipe_compatible(self, recipe: Recipe | None) -> None:
        """Reject recipes that would strand buffered products or connected ports."""
        if recipe is None:
            if any(self.input_buffers):
                raise ConflictError("cannot clear recipe while input buffers hold products")
            return
        for port in self.inputs:
            if IN_PORTS.index(port) >= len(recipe.inputs):
                raise ConflictError(f"recipe '{recipe.id}' has no slot for connected input port {port}")
        for port in self.outputs:
            if OUT_PORTS.index(port) >= len(recipe.outputs):
                raise ConflictError(f"recipe '{recipe.id}' has no slot for connected output port {port}")
        for i, buf in enumerate(self.input_buffers):
            for pid in buf:
                ptype = self.sim.products[pid].product_type_id
                if i >= len(recipe.inputs) or recipe.inputs[i].product_type_id != ptype:
                    raise ConflictError(
                        f"input buffer {IN_PORTS[i]} holds '{ptype}' products incompatible with recipe '{recipe.id}'")

    def check_config_change(self, old: dict, new: dict) -> None:
        if old.get("recipe_id") != new.get("recipe_id"):
            self.check_recipe_compatible(self.sim.recipes.get(new["recipe_id"]) if new["recipe_id"] else None)

    def on_config_changed(self, old: dict, new: dict) -> None:
        if self.state == UNCONFIGURED and self.recipe is not None:
            self.set_state(IDLE, "recipe selected")
        elif self.is_operational() and self.recipe is None and not self.active_cycle_id:
            self.set_state(UNCONFIGURED, "no recipe selected")
        else:
            self.record_conditions_change()
        super().on_config_changed(old, new)
        self.wake_inputs()

    def contained(self) -> Iterable[tuple[str, str]]:
        for buf in self.input_buffers:
            for pid in buf:
                yield pid, "input_buffer"
        if self.active_cycle_id:
            for pid in self.sim.cycles[self.active_cycle_id].input_ids:
                yield pid, "cycle"
        for buf in self.output_buffers:
            for pid in buf:
                yield pid, "output_buffer"

    # ------------------------------------------------------------------ input side
    def input_capacity(self, slot: int) -> int:
        r = self.recipe
        return self.config["input_buffer_multiplier"] * r.inputs[slot].quantity if r else 0

    def output_capacity(self, slot: int, recipe: Recipe | None = None) -> int:
        r = recipe or self.recipe
        return self.config["output_buffer_multiplier"] * r.outputs[slot].quantity if r else 0

    def can_accept(self, port: str, product: Product) -> tuple[bool, str | None]:
        if not self.is_operational():
            return False, f"transformer {self.state}"
        r = self.recipe
        if r is None:
            return False, "transformer has no recipe"
        slot = IN_PORTS.index(port)
        if slot >= len(r.inputs):
            return False, f"recipe has no input slot for {port}"
        if r.inputs[slot].product_type_id != product.product_type_id:
            return False, (f"incompatible product type '{product.product_type_id}' for {port} "
                           f"(expects '{r.inputs[slot].product_type_id}')")
        if len(self.input_buffers[slot]) >= self.input_capacity(slot):
            return False, f"input buffer {port} full"
        return True, None

    def accept(self, port: str, product: Product) -> None:
        slot = IN_PORTS.index(port)
        self.input_buffers[slot].append(product.id)
        self.place(product, "input_buffer", port, ProductStatus.QUEUED)
        self.sim.request_wake(self.id)

    # ------------------------------------------------------------------ cycle logic
    def wake(self) -> None:
        if not self.is_operational():
            return
        moved_out = False
        for i, buf in enumerate(self.output_buffers):
            moved_out |= self.push_queue(OUT_PORTS[i], buf)
        if self.active_cycle_id:
            return
        r = self.recipe
        if r is None:
            self.set_state(UNCONFIGURED, "no recipe selected")
            return
        missing = [IN_PORTS[i] for i, item in enumerate(r.inputs)
                   if len(self.input_buffers[i]) < item.quantity]
        full = [OUT_PORTS[j] for j, item in enumerate(r.outputs)
                if len(self.output_buffers[j]) + item.quantity > self.output_capacity(j)]
        if full:
            self.set_state(BLOCKED, f"output buffer full: {', '.join(full)}")
            return
        if missing:
            self.set_state(IDLE, f"waiting for inputs: {', '.join(missing)}")
            return
        self._start_cycle(r)

    def _start_cycle(self, recipe: Recipe) -> None:
        sim = self.sim
        speed = self.config["speed_pct"]
        mult = self.time_multiplier(speed)
        quality = self.quality_score(speed)
        duration = recipe.processing_time_s * mult
        reserved: list[str] = []
        for i, item in enumerate(recipe.inputs):
            buf = self.input_buffers[i]
            for _ in range(item.quantity):
                reserved.append(buf.popleft())
        orders: list[str] = []
        for pid in reserved:
            for o in sim.products[pid].production_order_ids:
                if o not in orders:
                    orders.append(o)
        self.cycle_seq += 1
        lot = f"LOT-{self.id}-{self.cycle_seq:05d}"
        snapshot = {"recipe": recipe.to_dict(), "speed_pct": speed, "time_multiplier": mult,
                    "processing_time_s": duration, "quality_score": quality,
                    "defect_probability": defect_probability(quality),
                    "quality_model": dict(self.config["quality_model"]),
                    "speed_model": dict(self.config["speed_model"])}
        cycle = sim.new_cycle(self, "transformation", lot, orders, snapshot, recipe_id=recipe.id)
        cycle.input_ids = list(reserved)
        cycle.scheduled_end = sim.now + duration
        self.active_cycle_id = cycle.id
        for pid in reserved:
            p = sim.products[pid]
            self.place(p, "cycle", None, ProductStatus.RESERVED)
            self._reservation_entries[pid] = sim.new_history_entry(
                p, self, start=sim.now, cycle_id=cycle.id,
                attributes={"role": "input", "recipe_id": recipe.id, "transformer_speed": speed / 100.0})
        self._completion_event = sim.scheduler.schedule(cycle.scheduled_end, Priority.COMPLETION,
                                                        "cycle_complete", self.id, cycle.id)
        self.set_state(PROCESSING, f"cycle {cycle.id}")
        sim.emit("processing_started", self, cycle_id=cycle.id, production_order_ids=orders,
                 input_product_ids=reserved, input_quantity=len(reserved), timestamp_start=sim.now,
                 config={"recipe_id": recipe.id, "speed_pct": speed, "time_multiplier": mult,
                         "config_version": self.config_version},
                 readings=self._readings(speed),
                 details={"scheduled_end": cycle.scheduled_end, "lot_number": lot,
                          "input_defects": sum(sim.products[p].is_defective for p in reserved)})
        self.wake_inputs()

    def _readings(self, speed: float) -> dict:
        return {"speed_pct": speed, "load_factor": round(speed / 100.0, 4),
                "power_kw": round(2.0 + 18.0 * (speed / 100.0) ** 1.5, 3)}

    def on_event(self, kind: str, payload: Any) -> None:
        if kind == "cycle_complete" and payload == self.active_cycle_id:
            self._complete_cycle(self.sim.cycles[payload])

    def _complete_cycle(self, cycle: ProductionCycle) -> None:
        sim = self.sim
        snap = cycle.config_snapshot
        recipe = Recipe.from_dict(snap["recipe"])
        quality = snap["quality_score"]
        p_defect = snap["defect_probability"]
        inputs = [sim.products[pid] for pid in cycle.input_ids]
        any_defective_input = any(p.is_defective for p in inputs)
        for p in inputs:
            self._reservation_entries.pop(p.id).complete(sim.now, result="consumed")
            sim.retire_product(p, ProductStatus.CONSUMED)
        outputs: list[str] = []
        defects = 0
        parent_ids = [p.id for p in inputs]
        for j, item in enumerate(recipe.outputs):
            for _ in range(item.quantity):
                if recipe.defect_policy == "any_defective_input" and any_defective_input:
                    defective = True
                else:
                    defective = sim.rng.random() < p_defect
                defects += int(defective)
                prod = sim.create_product(
                    type_id=item.product_type_id, lot=cycle.lot_number, order_ids=list(cycle.production_order_ids),
                    cycle_id=cycle.id, parent_ids=parent_ids, is_defective=defective,
                    quality_score=quality, attributes={"is_byproduct": item.is_byproduct} if item.is_byproduct else {},
                )
                prod.measurements["quality_score"] = quality
                self.place(prod, "output_buffer", OUT_PORTS[j], ProductStatus.QUEUED)
                entry = sim.new_history_entry(
                    prod, self, start=cycle.started_at, cycle_id=cycle.id,
                    attributes={"recipe_id": recipe.id, "transformer_speed": snap["speed_pct"] / 100.0,
                                "time_multiplier": snap["time_multiplier"], "quality_score": quality,
                                "defect_probability": p_defect, "is_defective": defective,
                                "output_slot": j, "is_byproduct": item.is_byproduct,
                                "input_defect_states": {p.id: p.is_defective for p in inputs},
                                "readings": self._readings(snap["speed_pct"]),
                                "config_version": cycle.config_version, "role": "output"})
                entry.complete(sim.now)
                self.output_buffers[j].append(prod.id)
                outputs.append(prod.id)
        sim.finish_cycle(cycle, input_ids=cycle.input_ids, output_ids=outputs, defect_count=defects)
        c = self.stats_counters
        c["cycles_completed"] += 1
        c["inputs_consumed"] += len(inputs)
        c["outputs_generated"] += len(outputs)
        c["defective_outputs"] += defects
        c["busy_time_s"] += sim.now - cycle.started_at
        self.active_cycle_id = None
        self._completion_event = None
        sim.emit("processing_completed", self, cycle_id=cycle.id,
                 production_order_ids=list(cycle.production_order_ids),
                 input_product_ids=list(cycle.input_ids), output_product_ids=outputs,
                 input_quantity=len(inputs), output_quantity=len(outputs), defect_count=defects,
                 timestamp_start=cycle.started_at, timestamp_end=sim.now,
                 config={"recipe_id": recipe.id, "speed_pct": snap["speed_pct"],
                         "config_version": cycle.config_version},
                 readings={**self._readings(snap["speed_pct"]), "quality_score": quality},
                 details={"lot_number": cycle.lot_number,
                          "input_defects": sum(p.is_defective for p in inputs)})
        self.set_state(IDLE, "cycle completed")
        self.wake()

    # ------------------------------------------------------------------ cancellation
    def on_stop(self, reason: str) -> None:
        if not self.active_cycle_id:
            return
        sim = self.sim
        cycle = sim.cycles[self.active_cycle_id]
        sim.scheduler.cancel(self._completion_event)
        recipe = Recipe.from_dict(cycle.config_snapshot["recipe"])
        # Restore reserved inputs to the front of their buffers in original order.
        idx = 0
        per_slot: list[list[str]] = []
        for item in recipe.inputs:
            per_slot.append(cycle.input_ids[idx: idx + item.quantity])
            idx += item.quantity
        for i, pids in enumerate(per_slot):
            for pid in reversed(pids):
                self.input_buffers[i].appendleft(pid)
                p = sim.products[pid]
                self.place(p, "input_buffer", IN_PORTS[i], ProductStatus.QUEUED)
                self._reservation_entries.pop(pid).complete(sim.now, result="cancelled", reason=reason)
        cycle.status = "cancelled"
        cycle.ended_at = sim.now
        cycle.end_reason = reason
        self.stats_counters["cycles_cancelled"] += 1
        self.active_cycle_id = None
        self._completion_event = None
        sim.emit("cycle_cancelled", self, cycle_id=cycle.id, production_order_ids=list(cycle.production_order_ids),
                 input_product_ids=list(cycle.input_ids), input_quantity=len(cycle.input_ids),
                 timestamp_start=cycle.started_at, timestamp_end=sim.now, error=reason,
                 details={"inputs_restored": True})

    def on_restart(self) -> None:
        if self.recipe is None:
            self.set_state(UNCONFIGURED, "no recipe selected")

    # ------------------------------------------------------------------ views
    def counts(self) -> dict:
        r = self.recipe
        n_in = len(r.inputs) if r else 0
        n_out = len(r.outputs) if r else 0
        return {"inputs": [len(b) for b in self.input_buffers[:n_in]],
                "outputs": [len(b) for b in self.output_buffers[:n_out]],
                "active_cycle_id": self.active_cycle_id,
                "cycle_progress": self._progress()}

    def _progress(self) -> float | None:
        if not self.active_cycle_id:
            return None
        c = self.sim.cycles[self.active_cycle_id]
        total = (c.scheduled_end or c.started_at) - c.started_at
        return 1.0 if total <= 0 else max(0.0, min(1.0, (self.sim.now - c.started_at) / total))

    def stats(self) -> dict:
        s = dict(self.stats_counters)
        s["utilization"] = (s["busy_time_s"] / self.sim.now) if self.sim.now > 0 else None
        s["current_quality_score"] = self.quality_score()
        s["current_time_multiplier"] = self.time_multiplier()
        s["active_cycle"] = self.sim.cycles[self.active_cycle_id].to_dict() if self.active_cycle_id else None
        return s
