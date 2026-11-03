"""Resource producers: generate batches of products at a fixed interval.

Blocked-output policy: generated products are kept in an output queue until the output
conveyor has space. When the queue already holds ``max_pending_batches`` batches worth of
products, the generation tick is *skipped* (recorded as a ``production_skipped`` event) and
the next tick is still scheduled one interval later. Pending output is therefore bounded and
no product is ever lost or duplicated.

The first batch is generated ``start_delay_s`` after the simulation starts (default 0).
Interval changes apply after the already scheduled next tick.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from ..errors import ConflictError, ValidationError
from ..ids import validate_id
from ..models import ProductStatus, require_positive_int, require_positive_number
from ..scheduler import Priority
from .base import BLOCKED, FAILED, IDLE, RUNNING, STOPPED, Asset, parse_initial_state

UNIT_SECONDS = {"seconds": 1.0, "minutes": 60.0, "hours": 3600.0}


class Producer(Asset):
    kind = "producer"
    output_port_names = ("out",)

    def __init__(self, sim, asset_id, name, config, position=None) -> None:
        self.pending: deque[str] = deque()
        self.batch_seq = 0
        self.generated_count = 0
        self.skipped_ticks = 0
        self._tick_event: int | None = None
        super().__init__(sim, asset_id, name, config, position)

    @classmethod
    def parse_config(cls, raw: dict, sim) -> dict:
        ptid = validate_id(raw.get("product_type_id"), "product_type_id")
        if ptid not in sim.product_types:
            raise ValidationError(f"unknown product type '{ptid}'")
        unit = raw.get("interval_unit", "seconds")
        if unit not in UNIT_SECONDS:
            raise ValidationError(f"interval_unit must be one of {list(UNIT_SECONDS)}")
        order = raw.get("production_order_id") or sim.default_order_id
        if order not in sim.orders:
            raise ValidationError(f"unknown production order '{order}'")
        dp = raw.get("defect_probability", 0.0)
        if isinstance(dp, bool) or not isinstance(dp, (int, float)) or not (0.0 <= dp <= 1.0):
            raise ValidationError("defect_probability must be within [0, 1]")
        delay = raw.get("start_delay_s", 0.0)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0:
            raise ValidationError("start_delay_s must be >= 0")
        attrs = raw.get("attributes") or {}
        if not isinstance(attrs, dict):
            raise ValidationError("attributes must be an object")
        from ..industrial.definitions import normalize_mappings
        mappings = normalize_mappings(raw.get("attribute_mappings"))
        return {
            "product_type_id": ptid,
            "batch_quantity": require_positive_int(raw.get("batch_quantity", 1), "batch_quantity"),
            "interval": require_positive_number(raw.get("interval", 10.0), "interval"),
            "interval_unit": unit,
            "max_pending_batches": require_positive_int(raw.get("max_pending_batches", 1), "max_pending_batches"),
            "production_order_id": order,
            "defect_probability": float(dp),
            "start_delay_s": float(delay),
            "attributes": dict(attrs),
            "attribute_mappings": mappings,
            "initial_state": parse_initial_state(raw),
        }

    def effective_boundary(self, changed: set[str]) -> str:
        if changed & {"interval", "interval_unit"}:
            return "interval applies after the already scheduled next batch"
        return "next generated batch"

    @property
    def interval_s(self) -> float:
        return self.config["interval"] * UNIT_SECONDS[self.config["interval_unit"]]

    def conditions(self) -> dict:
        return {"config_version": self.config_version, "batch_quantity": self.config["batch_quantity"],
                "interval_s": self.interval_s}

    def contained(self) -> Iterable[tuple[str, str]]:
        for pid in self.pending:
            yield pid, "output_buffer"

    # ------------------------------------------------------------------ scheduling
    def activate(self) -> None:
        if self.is_operational():
            if self.sim.industrial.production_external(self.id):
                self.set_state(IDLE, "awaiting external production")
                return
            self.set_state(RUNNING)
            self._schedule_tick(self.sim.now + self.config["start_delay_s"])

    def _schedule_tick(self, at: float) -> None:
        self.sim.scheduler.cancel(self._tick_event)
        self._tick_event = self.sim.scheduler.schedule(at, Priority.PRODUCER_TICK, "tick", self.id)

    def on_stop(self, reason: str) -> None:
        self.sim.scheduler.cancel(self._tick_event)
        self._tick_event = None

    def on_restart(self) -> None:
        if self.sim.started and not self.sim.industrial.production_external(self.id):
            self._schedule_tick(self.sim.now)

    def on_sync_changed(self) -> None:
        if self.sim.industrial.production_external(self.id):
            self.on_stop("external production")
        elif self.sim.started and self.is_operational() and self._tick_event is None:
            self._schedule_tick(self.sim.now)

    def on_event(self, kind: str, payload: Any) -> None:
        if kind != "tick":
            return
        self._tick_event = None
        if not self.is_operational():
            return
        hub = self.sim.industrial
        if hub.production_external(self.id):
            return
        ind = hub.cycle_features(self)
        interval = ind["features"]["interval_s"] if ind else self.interval_s
        self._schedule_tick(self.sim.now + interval)
        blocking = hub.blocking_condition(self.id)
        if blocking:
            self.skipped_ticks += 1
            self.sim.emit("production_skipped", self, error=f"blocked by working condition '{blocking}'",
                          details={"condition_id": blocking})
            self.wake()
            return
        qty = ind["features"]["batch_quantity"] if ind else self.config["batch_quantity"]
        limit = self.config["max_pending_batches"] * self.config["batch_quantity"]
        if len(self.pending) >= limit:
            self.skipped_ticks += 1
            self.sim.emit("production_skipped", self, error="output blocked: pending output limit reached",
                          details={"pending": len(self.pending), "limit": limit})
            self.wake()
            return
        self._generate_batch(ind, quantity=qty)
        self.wake()

    def _generate_batch(self, ind: dict | None = None, *, quantity: int | None = None,
                        external: dict | None = None) -> list[str]:
        sim = self.sim
        hub = sim.industrial
        cfg = self.config
        self.batch_seq += 1
        lot = f"LOT-{self.id}-{self.batch_seq:05d}"
        order = cfg["production_order_id"]
        snapshot = {k: v for k, v in cfg.items() if k != "initial_state"}
        snapshot["interval_s"] = self.interval_s
        p_defect = cfg["defect_probability"]
        if ind is not None:
            snapshot["industrial"] = ind
            snapshot["interval_s"] = ind["features"]["interval_s"]
            snapshot["batch_quantity"] = ind["features"]["batch_quantity"]
            p_defect = snapshot["defect_probability"] = ind["features"]["defect_probability"]
        specs = list((external or {}).get("outputs") or [])
        qty = len(specs) if specs else (quantity or cfg["batch_quantity"])
        if specs:
            snapshot["batch_quantity"] = qty
        cycle = sim.new_cycle(self, "producer_batch", lot, [order], snapshot)
        if external is not None:
            cycle.source = "external"
            cycle.external_cycle_id = external.get("cycle_id")
            cycle.external_timestamps = {"completed": external.get("timestamp")}
            cycle.exceptions = list(external.get("exceptions") or [])
        if ind is not None:
            hub.on_cycle_started(self, cycle)
        details = {"lot_number": lot, "batch_index": self.batch_seq}
        if cycle.source == "external":
            details.update(source="external", external_cycle_id=cycle.external_cycle_id)
        sim.emit("production_started", self, cycle_id=cycle.id, production_order_ids=[order],
                 config=snapshot, details=details)
        if hub.active(self.id):
            hub.on_cycle_ending(self, cycle)
        pt = sim.product_types.get(cfg["product_type_id"])
        map_attrs = bool(cfg["attribute_mappings"] or (pt is not None and pt.attributes))
        features = {"interval_s": snapshot["interval_s"], "batch_quantity": qty, "defect_probability": p_defect}
        ids = []
        defects = 0
        for u in range(qty):
            spec = specs[u] if specs else {}
            if isinstance(spec.get("is_defective"), bool):
                defective = spec["is_defective"]
            else:
                defective = p_defect > 0 and sim.rng.random() < p_defect
            defects += int(defective)
            provenance = None
            if map_attrs or spec.get("attributes"):
                attrs, provenance = hub.resolve_attributes(
                    self, product_type_id=cfg["product_type_id"], mappings=cfg["attribute_mappings"], cycle=cycle,
                    features=features, static=cfg["attributes"], slot=0, unit_index=u,
                    external=spec.get("attributes"))
            else:
                attrs = dict(cfg["attributes"])
            product = sim.create_product(
                type_id=cfg["product_type_id"], lot=lot, order_ids=[order], cycle_id=cycle.id,
                parent_ids=[], is_defective=defective, attributes=attrs, attribute_provenance=provenance,
                external_id=spec.get("product_id"),
            )
            self.place(product, "output_buffer", "out", ProductStatus.QUEUED)
            hist = {"interval_s": snapshot["interval_s"], "batch_quantity": qty,
                    "batch_index": self.batch_seq, "lot_number": lot, "is_defective": defective,
                    "config_version": self.config_version}
            if ind is not None:
                hist.update(working_conditions=ind["working_conditions"], classification=ind["classification"])
            entry = sim.new_history_entry(product, self, start=sim.now, cycle_id=cycle.id, attributes=hist)
            entry.complete(sim.now)
            self.pending.append(product.id)
            ids.append(product.id)
        self.generated_count += len(ids)
        sim.finish_cycle(cycle, input_ids=[], output_ids=ids, defect_count=defects)
        sim.emit("product_generated", self, cycle_id=cycle.id, production_order_ids=[order],
                 output_product_ids=ids, output_quantity=len(ids), defect_count=defects,
                 timestamp_start=sim.now, timestamp_end=sim.now,
                 details={k: v for k, v in details.items() if k != "batch_index"})
        return ids

    def produce_external_batch(self, *, cycle_id: str | None, outputs: list[dict] | None, strict: bool,
                               timestamp: Any = None) -> list[str]:
        """Record a batch reported by an external system (validated before any mutation)."""
        if not self.is_operational():
            raise ConflictError(f"producer '{self.id}' is {self.state}")
        exceptions = []
        for k, spec in enumerate(outputs or []):
            t = spec.get("product_type_id")
            if t is not None and t != self.config["product_type_id"]:
                exceptions.append(f"output #{k} type '{t}' differs from configured '{self.config['product_type_id']}'")
        if exceptions and strict:
            raise ValidationError("external batch does not match the producer: " + "; ".join(exceptions))
        ind = self.sim.industrial.cycle_features(self)
        ids = self._generate_batch(ind, external={"cycle_id": cycle_id, "outputs": outputs, "timestamp": timestamp,
                                                  "exceptions": exceptions})
        self.wake()
        return ids

    def wake(self) -> None:
        if not self.is_operational():
            return
        self.push_queue("out", self.pending)
        if self.pending:
            reason = "no output conveyor" if "out" not in self.outputs else "output conveyor full"
            self.set_state(BLOCKED, reason)
        elif self.sim.industrial.production_external(self.id):
            self.set_state(IDLE, "awaiting external production")
        elif (blocking := self.sim.industrial.blocking_condition(self.id)):
            self.set_state(BLOCKED, f"working condition '{blocking}'")
        else:
            self.set_state(RUNNING if self.sim.started else IDLE)

    # ------------------------------------------------------------------ views
    def counts(self) -> dict:
        return {"pending": len(self.pending)}

    def stats(self) -> dict:
        return {"batches": self.batch_seq, "generated": self.generated_count,
                "pending": len(self.pending), "skipped_ticks": self.skipped_ticks,
                "interval_s": self.interval_s}


__all__ = ["Producer", "IDLE", "STOPPED", "FAILED"]
