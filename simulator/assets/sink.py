"""Sinks: accept products, decide completion, and retain them for inspection.

* ``accepted_product_type_ids``: when set, other product types are *rejected*; the product
  stays at the head of the incoming conveyor (backpressure) and a ``product_rejected`` event
  is logged. Nothing is discarded.
* ``capacity``: optional maximum number of retained products; a full sink blocks its inputs.
* ``completion_criteria``: list of conditions (all must match). Accepted products meeting
  them become ``completed``; others are retained as ``stored`` (e.g. scrapped/defective).
Both are terminal: the product leaves the active production network but its identity,
attributes, lineage and history remain queryable.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..conditions import evaluate, validate_condition
from ..errors import ValidationError
from ..models import Location, Product, ProductStatus
from .base import BLOCKED, IDLE, RUNNING, Asset, parse_initial_state

SINK_PORTS = ("in0", "in1", "in2", "in3")


class Sink(Asset):
    kind = "sink"
    input_port_names = SINK_PORTS

    def __init__(self, sim, asset_id, name, config, position=None) -> None:
        self.received: list[str] = []
        super().__init__(sim, asset_id, name, config, position)

    @classmethod
    def parse_config(cls, raw: dict, sim) -> dict:
        accepted = raw.get("accepted_product_type_ids")
        if accepted is not None:
            if not isinstance(accepted, list) or not all(isinstance(a, str) for a in accepted):
                raise ValidationError("accepted_product_type_ids must be a list of product type IDs or null")
            unknown = [a for a in accepted if a not in sim.product_types]
            if unknown:
                raise ValidationError(f"unknown product types in accepted_product_type_ids: {unknown}")
        cap = raw.get("capacity")
        if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0):
            raise ValidationError("capacity must be a positive integer or null")
        crit_raw = raw.get("completion_criteria") or []
        if not isinstance(crit_raw, list):
            raise ValidationError("completion_criteria must be a list")
        criteria = [validate_condition(c, f"completion_criteria[{i}]") for i, c in enumerate(crit_raw)]
        return {"accepted_product_type_ids": accepted, "capacity": cap, "completion_criteria": criteria,
                "initial_state": parse_initial_state(raw)}

    def effective_boundary(self, changed: set[str]) -> str:
        return "products arriving after the change; already received products are unchanged"

    def can_accept(self, port: str, product: Product) -> tuple[bool, str | None]:
        if not self.is_operational():
            return False, f"sink {self.state}"
        accepted = self.config["accepted_product_type_ids"]
        if accepted is not None and product.product_type_id not in accepted:
            return False, f"rejected: product type '{product.product_type_id}' not accepted by sink"
        cap = self.config["capacity"]
        if cap is not None and len(self.received) >= cap:
            return False, "sink full"
        return True, None

    def accept(self, port: str, product: Product) -> None:
        sim = self.sim
        results = []
        completed = True
        for c in self.config["completion_criteria"]:
            matched, missing = evaluate(c, product)
            results.append({"condition": dict(c), "matched": matched, "missing": missing})
            completed &= matched
        status = ProductStatus.COMPLETED if completed else ProductStatus.STORED
        product.location = Location(self.id, "sink", port)
        entry = sim.new_history_entry(product, self, start=sim.now,
                                      attributes={"completed": completed, "criteria_results": results,
                                                  "is_defective": product.is_defective,
                                                  "config_version": self.config_version})
        entry.complete(sim.now)
        sim.retire_product(product, status, keep_location=True)
        self.received.append(product.id)
        sim.emit("product_completed" if completed else "product_stored", self,
                 production_order_ids=list(product.production_order_ids), cycle_id=product.origin_cycle_id,
                 input_product_ids=[product.id], input_quantity=1, defect_count=int(product.is_defective),
                 details={"product_type_id": product.product_type_id, "lot_number": product.lot_number,
                          "completed": completed})
        sim.request_wake(self.id)

    def wake(self) -> None:
        if not self.is_operational():
            return
        cap = self.config["capacity"]
        if cap is not None and len(self.received) >= cap:
            self.set_state(BLOCKED, "sink full")
        else:
            self.set_state(RUNNING if self.received else IDLE)

    def on_config_changed(self, old: dict, new: dict) -> None:
        super().on_config_changed(old, new)
        self.wake_inputs()

    # ------------------------------------------------------------------ queries
    def products(self, *, completed: bool | None = None, defective: bool | None = None,
                 product_type_id: str | None = None, lot_number: str | None = None,
                 order_id: str | None = None, cycle_id: str | None = None,
                 offset: int = 0, limit: int = 100) -> list[Product]:
        out = []
        for pid in self.received:
            p = self.sim.products[pid]
            if completed is not None and (p.status == ProductStatus.COMPLETED) != completed:
                continue
            if defective is not None and p.is_defective != defective:
                continue
            if product_type_id and p.product_type_id != product_type_id:
                continue
            if lot_number and p.lot_number != lot_number:
                continue
            if order_id and order_id not in p.production_order_ids:
                continue
            if cycle_id and p.origin_cycle_id != cycle_id:
                continue
            out.append(p)
        return out[offset: offset + limit]

    def counts(self) -> dict:
        return {"received": len(self.received)}

    def stats(self) -> dict:
        prods = [self.sim.products[pid] for pid in self.received]
        completed = sum(p.status == ProductStatus.COMPLETED for p in prods)
        defective = sum(p.is_defective for p in prods)
        times = [p.terminal_at for p in prods if p.terminal_at is not None]
        orders: Counter[str] = Counter()
        for p in prods:
            orders.update(p.production_order_ids)
        return {
            "total_received": len(prods), "total_completed": completed, "total_stored": len(prods) - completed,
            "defective": defective, "non_defective": len(prods) - defective,
            "by_product_type": dict(Counter(p.product_type_id for p in prods)),
            "by_lot": dict(Counter(p.lot_number for p in prods)),
            "by_production_order": dict(orders),
            "by_origin_cycle": dict(Counter(p.origin_cycle_id for p in prods)),
            "first_completion_at": min(times) if times else None,
            "last_completion_at": max(times) if times else None,
        }

    def on_event(self, kind: str, payload: Any) -> None:
        pass
