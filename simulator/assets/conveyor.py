"""Conveyor belts: bounded FIFO transport queues with explicit travel time.

Semantics (documented in docs/ARCHITECTURE.md):

* ``capacity = floor(length_m * capacity_per_m)`` (must be >= 1). Capacity counts *every*
  product on the belt: products still travelling and products waiting at the head.
* A product admitted at time ``t`` reaches the head at ``max(t + length/speed, arrival of the
  product ahead)`` (FIFO, no overtaking). It is never delivered before that time.
* A product at the head is delivered only if the downstream asset accepts it; otherwise the
  belt is ``blocked`` (backpressure) and nothing behind it is delivered.
* Changing ``speed_m_s`` or ``length_m`` only affects products admitted afterwards; products in
  transit keep their scheduled arrival time.
* Stopping a belt freezes products in place: their remaining travel time is stored and
  re-applied on restart.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

from ..errors import ValidationError
from ..models import HistoryEntry, Product, ProductStatus, require_positive_number
from ..scheduler import Priority
from .base import BLOCKED, IDLE, RUNNING, STOPPED, FAILED, Asset, parse_initial_state

EPS = 1e-9


@dataclass
class ConveyorItem:
    product_id: str
    admitted_at: float
    arrival_at: float
    entry: HistoryEntry
    remaining: float | None = None   # remaining travel time while the belt is stopped


class Conveyor(Asset):
    kind = "conveyor"

    def __init__(self, sim, asset_id, name, config, position=None, *, source: tuple[str, str],
                 target: tuple[str, str]) -> None:
        self.source = source
        self.target = target
        self.items: deque[ConveyorItem] = deque()
        self.delivered_count = 0
        self.admitted_count = 0
        self._rejection_reason: str | None = None
        super().__init__(sim, asset_id, name, config, position)

    @classmethod
    def parse_config(cls, raw: dict, sim) -> dict:
        length = require_positive_number(raw.get("length_m", 10.0), "length_m")
        cpm = require_positive_number(raw.get("capacity_per_m", 1.0), "capacity_per_m")
        speed = require_positive_number(raw.get("speed_m_s", 1.0), "speed_m_s")
        if math.floor(length * cpm + EPS) < 1:
            raise ValidationError("conveyor capacity (floor(length_m * capacity_per_m)) must be at least 1")
        return {"length_m": length, "capacity_per_m": cpm, "speed_m_s": speed,
                "initial_state": parse_initial_state(raw)}

    def effective_boundary(self, changed: set[str]) -> str:
        return "products admitted after the change; products in transit keep their scheduled arrival"

    # ------------------------------------------------------------------ properties
    @property
    def capacity(self) -> int:
        return math.floor(self.config["length_m"] * self.config["capacity_per_m"] + EPS)

    @property
    def travel_time(self) -> float:
        return self.config["length_m"] / self.config["speed_m_s"]

    def port_info(self) -> dict:
        return {"inputs": [], "outputs": [], "source": {"asset_id": self.source[0], "port": self.source[1]},
                "target": {"asset_id": self.target[0], "port": self.target[1]}}

    def has_space(self) -> bool:
        return self.is_operational() and len(self.items) < self.capacity

    def contained(self) -> Iterable[tuple[str, str]]:
        for it in self.items:
            yield it.product_id, "conveyor"

    # ------------------------------------------------------------------ transport
    def admit(self, product: Product) -> None:
        if not self.has_space():
            raise RuntimeError(f"conveyor {self.id} has no space")  # callers must check has_space()
        now = self.sim.now
        arrival = now + self.travel_time
        if self.items:
            arrival = max(arrival, self.items[-1].arrival_at)
        entry = self.sim.new_history_entry(
            product, self, start=now,
            attributes={"length_m": self.config["length_m"], "speed_m_s": self.config["speed_m_s"],
                        "scheduled_arrival": arrival, "config_version": self.config_version},
        )
        self.items.append(ConveyorItem(product.id, now, arrival, entry))
        self.place(product, "conveyor", None, ProductStatus.IN_TRANSIT)
        self.admitted_count += 1
        self.sim.scheduler.schedule(arrival, Priority.ARRIVAL, "arrival", self.id, product.id)
        if self.state == IDLE:
            self.set_state(RUNNING)

    def on_event(self, kind: str, payload: Any) -> None:
        if kind == "arrival":
            self.wake()

    def wake(self) -> None:
        if not self.is_operational():
            return
        target = self.sim.assets.get(self.target[0])
        delivered = False
        blocked_reason = None
        while self.items and self.items[0].arrival_at <= self.sim.now + EPS:
            head = self.items[0]
            product = self.sim.products[head.product_id]
            ok, reason = target.can_accept(self.target[1], product) if target else (False, "no target")
            if not ok:
                blocked_reason = reason or "downstream unavailable"
                key = f"{product.id}|{blocked_reason}"
                if blocked_reason.startswith("rejected") and key != self._rejection_reason:
                    # Explicit rejection (e.g. sink acceptance rule): the product stays on the belt.
                    self.sim.emit("product_rejected", self, input_product_ids=[product.id],
                                  error=blocked_reason, details={"target": self.target[0]})
                    self._rejection_reason = key
                break
            self.items.popleft()
            head.entry.complete(self.sim.now, delivered_to=self.target[0])
            self.delivered_count += 1
            target.accept(self.target[1], product)
            delivered = True
        if delivered:
            self.sim.request_wake(self.source[0])
            self.sim.request_wake(self.target[0])
        if blocked_reason is not None:
            self.set_state(BLOCKED, blocked_reason)
        else:
            self.set_state(RUNNING if self.items else IDLE)

    # ------------------------------------------------------------------ stop / restart
    def on_stop(self, reason: str) -> None:
        now = self.sim.now
        for it in self.items:
            it.remaining = max(0.0, it.arrival_at - now)

    def on_restart(self) -> None:
        now = self.sim.now
        last = None
        for it in self.items:
            if it.remaining is not None:
                it.arrival_at = now + it.remaining
                it.remaining = None
            if last is not None:
                it.arrival_at = max(it.arrival_at, last)
            last = it.arrival_at
            self.sim.scheduler.schedule(it.arrival_at, Priority.ARRIVAL, "arrival", self.id, it.product_id)

    # ------------------------------------------------------------------ views
    def counts(self) -> dict:
        return {"occupancy": len(self.items), "capacity": self.capacity}

    def stats(self) -> dict:
        return {"admitted": self.admitted_count, "delivered": self.delivered_count,
                "occupancy": len(self.items), "capacity": self.capacity,
                "travel_time_s": self.travel_time}

    def item_positions(self, limit: int = 200) -> list[dict]:
        now = self.sim.now
        out = []
        for it in list(self.items)[:limit]:
            if it.remaining is not None:
                total = max(EPS, it.arrival_at - it.admitted_at)
                progress = 1.0 - it.remaining / total
            else:
                total = it.arrival_at - it.admitted_at
                progress = 1.0 if total <= EPS else (now - it.admitted_at) / total
            p = self.sim.products[it.product_id]
            out.append({"id": it.product_id, "progress": max(0.0, min(1.0, progress)),
                        "defective": p.is_defective, "type": p.product_type_id})
        return out

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["items"] = self.item_positions()
        snap["source"] = {"asset_id": self.source[0], "port": self.source[1]}
        snap["target"] = {"asset_id": self.target[0], "port": self.target[1]}
        return snap


__all__ = ["Conveyor", "ConveyorItem", "STOPPED", "FAILED"]
