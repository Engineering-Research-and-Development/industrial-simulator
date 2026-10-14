"""Simulation engine: registries, event loop, product/traceability bookkeeping, invariants.

The engine is UI- and broker-independent and advances only in *simulation time*. Wall-clock
pacing (acceleration, pause/resume) lives in :mod:`simulator.controller`.
"""

from __future__ import annotations

import copy
import random
from collections import Counter, deque
from typing import Any, Callable, Iterable

from .assets import ASSET_KINDS, Asset, Conveyor, Producer, Sink, Splitter, Transformer
from .errors import ConflictError, NotFoundError, ValidationError
from .ids import IdFactory, validate_id
from .models import (ACTIVE_STATUSES, ConfigChange, HistoryEntry, MachineEvent, OperationalInterval, Product,
                     ProductionCycle, ProductionOrder, ProductStatus, ProductType, Recipe)
from .scheduler import Priority, Scheduler

DEFAULT_ORDER_ID = "order-default"
MAX_EVENTS_PER_INSTANT = 2_000_000

Listener = Callable[[str, dict], None]


class Simulation:
    def __init__(self, seed: int | None = None, *, event_log_limit: int | None = None, name: str = "untitled") -> None:
        self.name = name
        self.seed = seed
        self.now = 0.0
        self.started = False
        self.rng = random.Random(seed)
        self.ids = IdFactory(seed)
        self.scheduler = Scheduler()
        self.product_types: dict[str, ProductType] = {}
        self.recipes: dict[str, Recipe] = {}
        self.orders: dict[str, ProductionOrder] = {}
        self.assets: dict[str, Asset] = {}
        self.products: dict[str, Product] = {}
        self.active_product_ids: set[str] = set()
        self.children: dict[str, list[str]] = {}
        self.cycles: dict[str, ProductionCycle] = {}
        self.events: deque[MachineEvent] | list[MachineEvent] = (
            deque(maxlen=event_log_limit) if event_log_limit else [])
        self.operational_history: dict[str, list[OperationalInterval]] = {}
        self.config_changes: list[ConfigChange] = []
        self.counters: Counter[str] = Counter()
        self.events_processed = 0
        self._event_seq = 0
        self._label_seq: Counter[str] = Counter()
        self._pending_wakes: set[str] = set()
        self.listeners: list[Listener] = []
        self.listener_errors = 0
        self.default_order_id = DEFAULT_ORDER_ID
        self.orders[DEFAULT_ORDER_ID] = ProductionOrder(DEFAULT_ORDER_ID, "Default production order")

    # ------------------------------------------------------------------ pickling (checkpoints)
    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state["listeners"] = []
        return state

    # ================================================================== registries
    def add_product_type(self, data: dict) -> ProductType:
        pt = ProductType.from_dict(data)
        if pt.id in self.product_types:
            raise ConflictError(f"product type '{pt.id}' already exists")
        self.product_types[pt.id] = pt
        return pt

    def update_product_type(self, type_id: str, data: dict) -> ProductType:
        current = self.get_product_type(type_id)
        if "id" in data and data["id"] != type_id:
            raise ValidationError("product type IDs are immutable")
        merged = {**current.to_dict(), **data, "id": type_id}
        pt = ProductType.from_dict(merged)
        self.product_types[type_id] = pt
        self._record_change(f"product-type:{type_id}", current.to_dict(), pt.to_dict(), 0, "immediate (descriptive)")
        return pt

    def delete_product_type(self, type_id: str) -> None:
        self.get_product_type(type_id)
        users = [r.id for r in self.recipes.values() if type_id in r.referenced_types()]
        users += [a.id for a in self.assets.values() if isinstance(a, Producer) and a.config["product_type_id"] == type_id]
        users += [a.id for a in self.assets.values() if isinstance(a, Sink)
                  and type_id in (a.config["accepted_product_type_ids"] or [])]
        if users or any(p.product_type_id == type_id for p in self.products.values()):
            raise ConflictError(f"product type '{type_id}' is in use ({', '.join(users) or 'existing products'})")
        del self.product_types[type_id]

    def get_product_type(self, type_id: str) -> ProductType:
        try:
            return self.product_types[type_id]
        except KeyError:
            raise NotFoundError(f"unknown product type '{type_id}'") from None

    def add_recipe(self, data: dict) -> Recipe:
        r = Recipe.from_dict(data, set(self.product_types))
        if r.id in self.recipes:
            raise ConflictError(f"recipe '{r.id}' already exists")
        self.recipes[r.id] = r
        return r

    def update_recipe(self, recipe_id: str, data: dict) -> Recipe:
        current = self.get_recipe(recipe_id)
        if "id" in data and data["id"] != recipe_id:
            raise ValidationError("recipe IDs are immutable")
        r = Recipe.from_dict({**current.to_dict(), **data, "id": recipe_id}, set(self.product_types))
        users = [a for a in self.assets.values() if isinstance(a, Transformer) and a.config["recipe_id"] == recipe_id]
        for t in users:
            t.check_recipe_compatible(r)
        self.recipes[recipe_id] = r
        self._record_change(f"recipe:{recipe_id}", current.to_dict(), r.to_dict(), 0,
                            "next cycle of every transformer using the recipe; active cycles keep their snapshot")
        for t in users:
            t.record_conditions_change()
            self.request_wake(t.id)
            t.wake_inputs()
        return r

    def delete_recipe(self, recipe_id: str) -> None:
        self.get_recipe(recipe_id)
        users = [a.id for a in self.assets.values() if isinstance(a, Transformer) and a.config["recipe_id"] == recipe_id]
        if users:
            raise ConflictError(f"recipe '{recipe_id}' is used by {', '.join(users)}")
        del self.recipes[recipe_id]

    def get_recipe(self, recipe_id: str) -> Recipe:
        try:
            return self.recipes[recipe_id]
        except KeyError:
            raise NotFoundError(f"unknown recipe '{recipe_id}'") from None

    def add_order(self, data: dict) -> ProductionOrder:
        o = ProductionOrder.from_dict(data)
        if o.id in self.orders:
            raise ConflictError(f"production order '{o.id}' already exists")
        self.orders[o.id] = o
        return o

    def update_order(self, order_id: str, data: dict) -> ProductionOrder:
        current = self.get_order(order_id)
        if "id" in data and data["id"] != order_id:
            raise ValidationError("production order IDs are immutable")
        o = ProductionOrder.from_dict({**current.to_dict(), **data, "id": order_id})
        self.orders[order_id] = o
        self._record_change(f"order:{order_id}", current.to_dict(), o.to_dict(), 0, "immediate (descriptive)")
        return o

    def delete_order(self, order_id: str) -> None:
        self.get_order(order_id)
        if order_id == self.default_order_id:
            raise ConflictError("the default production order cannot be deleted")
        users = [a.id for a in self.assets.values() if isinstance(a, Producer)
                 and a.config["production_order_id"] == order_id]
        if users or any(order_id in p.production_order_ids for p in self.products.values()):
            raise ConflictError(f"production order '{order_id}' is in use ({', '.join(users) or 'existing products'})")
        del self.orders[order_id]

    def get_order(self, order_id: str) -> ProductionOrder:
        try:
            return self.orders[order_id]
        except KeyError:
            raise NotFoundError(f"unknown production order '{order_id}'") from None

    # ================================================================== assets
    def _new_asset_id(self, kind: str) -> str:
        while True:
            candidate = self.ids.new(kind)[: len(kind) + 9]
            if candidate not in self.assets:
                return candidate

    def add_asset(self, kind: str, name: str, config: dict | None = None, *, asset_id: str | None = None,
                  position: dict | None = None) -> Asset:
        cls = ASSET_KINDS.get(kind)
        if cls is None:
            raise ValidationError(f"unknown asset kind '{kind}' (expected one of {list(ASSET_KINDS)} or conveyor)")
        aid = self._check_new_asset(asset_id, kind, name)
        asset = cls(self, aid, name.strip(), config or {}, position)
        self.assets[aid] = asset
        if self.started:
            asset.activate()
            self.request_wake(aid)
        return asset

    def _check_new_asset(self, asset_id: str | None, kind: str, name: Any) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("asset name must be a non-empty string")
        if asset_id is None:
            return self._new_asset_id(kind)
        validate_id(asset_id, "asset id")
        if asset_id in self.assets:
            raise ConflictError(f"asset '{asset_id}' already exists")
        self.ids.reserve(asset_id)
        return asset_id

    def add_conveyor(self, name: str, source: dict, target: dict, config: dict | None = None, *,
                     asset_id: str | None = None, position: dict | None = None) -> Conveyor:
        try:
            src_id, src_port = source["asset_id"], source["port"]
            dst_id, dst_port = target["asset_id"], target["port"]
        except (KeyError, TypeError):
            raise ValidationError("source and target must be {asset_id, port} objects") from None
        src, dst = self.get_asset(src_id), self.get_asset(dst_id)
        if isinstance(src, Conveyor) or isinstance(dst, Conveyor):
            raise ValidationError("conveyors connect machine ports; they cannot be chained directly")
        if src is dst:
            raise ValidationError("a conveyor cannot connect an asset to itself")
        src.validate_output_connection(src_port)
        dst.validate_input_connection(dst_port)
        aid = self._check_new_asset(asset_id, "conveyor", name)
        conv = Conveyor(self, aid, name.strip(), config or {}, position, source=(src_id, src_port),
                        target=(dst_id, dst_port))
        self.assets[aid] = conv
        src.outputs[src_port] = aid
        dst.inputs[dst_port] = aid
        self.request_wake(src_id)
        self.request_wake(dst_id)
        return conv

    def get_asset(self, asset_id: str) -> Asset:
        try:
            return self.assets[asset_id]
        except KeyError:
            raise NotFoundError(f"unknown asset '{asset_id}'") from None

    def remove_asset(self, asset_id: str) -> None:
        asset = self.get_asset(asset_id)
        if isinstance(asset, Conveyor):
            if asset.items:
                raise ConflictError(f"conveyor '{asset_id}' carries {len(asset.items)} product(s); cannot remove")
            src = self.assets.get(asset.source[0])
            dst = self.assets.get(asset.target[0])
            if src:
                src.outputs.pop(asset.source[1], None)
                self.request_wake(src.id)
            if dst:
                dst.inputs.pop(asset.target[1], None)
            del self.assets[asset_id]
            return
        if not asset.is_empty():
            raise ConflictError(f"asset '{asset_id}' holds active products; cannot remove")
        if isinstance(asset, Sink) and asset.received:
            raise ConflictError(f"sink '{asset_id}' retains products; reset the simulation before removing it")
        attached = list(asset.inputs.values()) + list(asset.outputs.values())
        busy = [c for c in attached if self.assets[c].items]  # type: ignore[attr-defined]
        if busy:
            raise ConflictError(f"attached conveyors carry products: {', '.join(busy)}")
        for c in attached:
            self.remove_asset(c)
        asset.on_stop("removed")
        del self.assets[asset_id]

    def rename_asset(self, asset_id: str, name: str) -> Asset:
        asset = self.get_asset(asset_id)
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("name must be a non-empty string")
        old = asset.name
        asset.name = name.strip()
        self._record_change(asset_id, {"name": old}, {"name": asset.name}, asset.config_version, "immediate (rename)")
        return asset

    def set_position(self, asset_id: str, position: dict) -> None:
        asset = self.get_asset(asset_id)
        try:
            asset.position = {"x": float(position["x"]), "y": float(position["y"])}
        except (KeyError, TypeError, ValueError):
            raise ValidationError("position must be {x: number, y: number}") from None

    def update_config(self, asset_id: str, patch: dict) -> dict:
        asset = self.get_asset(asset_id)
        if not isinstance(patch, dict) or not patch:
            raise ValidationError("configuration patch must be a non-empty object")
        forbidden = {"id", "asset_id", "name", "kind"} & set(patch)
        if forbidden:
            raise ValidationError(f"{sorted(forbidden)} cannot be changed through configuration; use rename")
        unknown = set(patch) - set(asset.config)
        if unknown:
            raise ValidationError(f"unknown configuration field(s) for {asset.kind}: {sorted(unknown)}")
        old = copy.deepcopy(asset.config)
        new = type(asset).parse_config({**old, **copy.deepcopy(patch)}, self)  # full validation, no mutation
        asset.check_config_change(old, new)
        changed = {k for k in new if old.get(k) != new[k]}
        if not changed:
            return {"asset_id": asset_id, "version": asset.config_version, "changes": {}}
        asset.config = new
        asset.config_version += 1
        boundary = asset.effective_boundary(changed)
        change = self._record_change(asset_id, old, new, asset.config_version, boundary)
        self.emit("configuration_changed", asset, config={k: new[k] for k in changed},
                  details={"changes": change.changes, "config_version": asset.config_version,
                           "effective": boundary})
        asset.on_config_changed(old, new)
        return change.to_dict()

    def _record_change(self, subject: str, old: dict, new: dict, version: int, effective: str) -> ConfigChange:
        changes = {k: {"old": copy.deepcopy(old.get(k)), "new": copy.deepcopy(new.get(k))}
                   for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)}
        change = ConfigChange(self.ids.new("CFG"), subject, self.now, version, changes, effective)
        self.config_changes.append(change)
        self._notify("config_change", change.to_dict())
        return change

    def command(self, asset_id: str, command: str) -> None:
        self.get_asset(asset_id).command(command)

    # ================================================================== event loop
    def start(self) -> None:
        if self.started:
            return
        self.started = True
        for asset in list(self.assets.values()):
            asset.activate()
        for aid in list(self.assets):
            self.request_wake(aid)

    def request_wake(self, asset_id: str) -> None:
        if asset_id in self._pending_wakes or asset_id not in self.assets:
            return
        self._pending_wakes.add(asset_id)
        self.scheduler.schedule(self.now, Priority.WAKE, "wake", asset_id)

    def step(self) -> bool:
        """Process the next scheduled event. Returns False when nothing is scheduled."""
        if not self.started:
            raise ConflictError("simulation has not been started")
        ev = self.scheduler.pop()
        if ev is None:
            return False
        self.now = max(self.now, ev.time)
        self._dispatch(ev)
        return True

    def run_until(self, t: float) -> int:
        if not self.started:
            raise ConflictError("simulation has not been started")
        processed = 0
        last_time, same_time = None, 0
        while True:
            nt = self.scheduler.peek_time()
            if nt is None or nt > t:
                break
            ev = self.scheduler.pop()
            if ev.time > self.now:
                self.now = ev.time
            if ev.time == last_time:
                same_time += 1
                if same_time > MAX_EVENTS_PER_INSTANT:
                    raise RuntimeError("event storm detected at a single instant; aborting")
            else:
                last_time, same_time = ev.time, 0
            self._dispatch(ev)
            processed += 1
        if t > self.now:
            self.now = t
        return processed

    def run_for(self, dt: float) -> int:
        return self.run_until(self.now + dt)

    def _dispatch(self, ev) -> None:
        self.events_processed += 1
        asset = self.assets.get(ev.target)
        if ev.kind == "wake":
            self._pending_wakes.discard(ev.target)
            if asset is not None:
                asset.wake()
            return
        if asset is not None:
            asset.on_event(ev.kind, ev.payload)

    def next_event_time(self) -> float | None:
        return self.scheduler.peek_time()

    # ================================================================== products & records
    def create_product(self, *, type_id: str, lot: str, order_ids: list[str], cycle_id: str,
                       parent_ids: list[str], is_defective: bool, quality_score: float | None = None,
                       attributes: dict | None = None) -> Product:
        self._label_seq[type_id] += 1
        p = Product(
            id=self.ids.new("PRD"), label=f"{type_id}-{self._label_seq[type_id]:06d}", product_type_id=type_id,
            lot_number=lot, production_timestamp=self.now, status=ProductStatus.QUEUED, location=None,
            is_defective=is_defective, production_order_ids=list(order_ids), origin_cycle_id=cycle_id,
            parent_ids=list(parent_ids), quality_score=quality_score, attributes=dict(attributes or {}),
        )
        self.products[p.id] = p
        self.active_product_ids.add(p.id)
        for parent in parent_ids:
            self.children.setdefault(parent, []).append(p.id)
        self.counters["created"] += 1
        return p

    def retire_product(self, product: Product, status: ProductStatus, keep_location: bool = True) -> None:
        if product.status not in ACTIVE_STATUSES:
            raise RuntimeError(f"product {product.id} is already {product.status.value}")
        product.status = status
        product.terminal_at = self.now
        self.active_product_ids.discard(product.id)
        self.counters[status.value] += 1

    def new_history_entry(self, product: Product, asset: Asset, *, start: float, attributes: dict | None = None,
                          cycle_id: str | None = None) -> HistoryEntry:
        self.counters["history_seq"] += 1
        entry = HistoryEntry(
            entry_id=f"{product.id}/h{len(product.history)}", seq=self.counters["history_seq"],
            product_id=product.id, case_id=product.primary_order_id, activity=asset.id,
            activity_type=asset.kind, timestamp_start=start, cycle_id=cycle_id,
            attributes={"activity_name": asset.name, **(attributes or {})},
        )
        product.history.append(entry)
        return entry

    def new_cycle(self, asset: Asset, kind: str, lot: str, order_ids: list[str], snapshot: dict,
                  recipe_id: str | None = None) -> ProductionCycle:
        # Callers pass a freshly built dict; asset configs are replaced on update (never mutated in
        # place), so the snapshot cannot change afterwards and no deep copy is needed.
        cycle = ProductionCycle(id=self.ids.new("CYC"), asset_id=asset.id, kind=kind, status="active",
                                started_at=self.now, lot_number=lot, production_order_ids=list(order_ids),
                                config_snapshot=snapshot, config_version=asset.config_version,
                                recipe_id=recipe_id)
        self.cycles[cycle.id] = cycle
        return cycle

    def finish_cycle(self, cycle: ProductionCycle, *, input_ids: list[str], output_ids: list[str],
                     defect_count: int) -> None:
        cycle.status = "completed"
        cycle.ended_at = self.now
        cycle.input_ids = list(input_ids)
        cycle.output_ids = list(output_ids)
        cycle.defect_count = defect_count
        cycle.end_reason = "completed"

    def emit(self, event_type: str, asset: Asset, **fields: Any) -> MachineEvent:
        self._event_seq += 1
        ev = MachineEvent(event_id=self.ids.new("EVT"), seq=self._event_seq, event_type=event_type,
                          asset_id=asset.id, activity=asset.name, timestamp=self.now, **fields)
        self.events.append(ev)
        if self.listeners:
            payload = ev.to_dict()
            payload["asset_kind"] = asset.kind
            self._notify("machine_event", payload)
        return ev

    def _notify(self, channel: str, payload: dict) -> None:
        for listener in list(self.listeners):
            try:
                listener(channel, payload)
            except Exception:  # external integrations must never corrupt the simulation
                self.listener_errors += 1

    # ================================================================== queries
    def get_product(self, product_id: str) -> Product:
        try:
            return self.products[product_id]
        except KeyError:
            raise NotFoundError(f"unknown product '{product_id}'") from None

    def ancestors(self, product_id: str) -> list[str]:
        seen: list[str] = []
        stack = list(self.get_product(product_id).parent_ids)
        visited: set[str] = set()
        while stack:
            pid = stack.pop()
            if pid in visited:
                continue
            visited.add(pid)
            seen.append(pid)
            stack.extend(self.products[pid].parent_ids)
        return seen

    def full_history(self, product_id: str) -> list[dict]:
        """Own history plus all inherited (ancestor) histories, flattened chronologically.

        Ordering: (timestamp_start, entry sequence number). The sequence number is global and
        monotonic, so ordering of entries with identical timestamps is deterministic.
        """
        product = self.get_product(product_id)
        entries: list[HistoryEntry] = list(product.history)
        for pid in self.ancestors(product_id):
            entries.extend(self.products[pid].history)
        entries.sort(key=lambda e: (e.timestamp_start, e.seq))
        out = []
        for e in entries:
            d = e.to_dict()
            d["inherited"] = e.product_id != product_id
            out.append(d)
        return out

    def lineage_tree(self, product_id: str, depth: int = 6) -> dict:
        p = self.get_product(product_id)
        node = {"id": p.id, "label": p.label, "product_type_id": p.product_type_id,
                "is_defective": p.is_defective, "origin_cycle_id": p.origin_cycle_id, "parents": []}
        if depth > 0:
            node["parents"] = [self.lineage_tree(pid, depth - 1) for pid in p.parent_ids]
        elif p.parent_ids:
            node["truncated"] = True
        return node

    def product_record(self, product_id: str) -> dict:
        """DPP-like structured digital record."""
        p = self.get_product(product_id)
        ancestors = self.ancestors(product_id)
        transformation_cycles = []
        for cid in dict.fromkeys([p.origin_cycle_id] + [self.products[a].origin_cycle_id for a in ancestors]):
            c = self.cycles.get(cid)
            if c:
                transformation_cycles.append({"cycle_id": c.id, "asset_id": c.asset_id, "kind": c.kind,
                                              "recipe_id": c.recipe_id, "started_at": c.started_at,
                                              "ended_at": c.ended_at, "lot_number": c.lot_number})
        transformation_cycles.sort(key=lambda c: (c["started_at"], c["cycle_id"]))
        upstream_orders = list(dict.fromkeys(
            p.production_order_ids + [o for a in ancestors for o in self.products[a].production_order_ids]))
        return {
            "identity": {"product_id": p.id, "label": p.label, "product_type_id": p.product_type_id,
                         "product_type_name": self.product_types[p.product_type_id].name
                         if p.product_type_id in self.product_types else None,
                         "lot_number": p.lot_number, "production_timestamp": p.production_timestamp},
            "current_state": {"status": p.status.value, "location": p.location.to_dict() if p.location else None,
                              "is_defective": p.is_defective, "quality_score": p.quality_score,
                              "measurements": dict(p.measurements), "terminal_at": p.terminal_at},
            "provenance": {"parent_product_ids": list(p.parent_ids), "ancestor_product_ids": ancestors,
                           "child_product_ids": list(self.children.get(p.id, [])),
                           "production_order_ids": list(p.production_order_ids),
                           "upstream_production_order_ids": upstream_orders,
                           "origin_cycle_id": p.origin_cycle_id, "operations": transformation_cycles},
            "history": self.full_history(product_id),
            "additional_attributes": dict(p.attributes),
            "lineage": self.lineage_tree(product_id),
        }

    def query_products(self, *, status: str | None = None, product_type_id: str | None = None,
                       asset_id: str | None = None, order_id: str | None = None, lot_number: str | None = None,
                       defective: bool | None = None, offset: int = 0, limit: int = 100) -> list[Product]:
        out = []
        for p in reversed(list(self.products.values())):
            if status and p.status.value != status:
                continue
            if product_type_id and p.product_type_id != product_type_id:
                continue
            if asset_id and (p.location is None or p.location.asset_id != asset_id):
                continue
            if order_id and order_id not in p.production_order_ids:
                continue
            if lot_number and p.lot_number != lot_number:
                continue
            if defective is not None and p.is_defective != defective:
                continue
            out.append(p)
            if len(out) >= offset + limit:
                break
        return out[offset:]

    def query_events(self, *, asset_id: str | None = None, event_type: str | None = None,
                     cycle_id: str | None = None, product_id: str | None = None, order_id: str | None = None,
                     since_seq: int | None = None, limit: int = 200) -> list[MachineEvent]:
        def match(e: MachineEvent) -> bool:
            return ((asset_id is None or e.asset_id == asset_id)
                    and (event_type is None or e.event_type == event_type)
                    and (cycle_id is None or e.cycle_id == cycle_id)
                    and (order_id is None or order_id in e.production_order_ids)
                    and (product_id is None or product_id in e.input_product_ids
                         or product_id in e.output_product_ids))

        if since_seq is not None:
            out = []
            for e in self.events:
                if e.seq > since_seq and match(e):
                    out.append(e)
                    if len(out) >= limit:
                        break
            return out
        out = []
        for e in reversed(self.events):
            if match(e):
                out.append(e)
                if len(out) >= limit:
                    break
        out.reverse()
        return out

    def operational_history_for(self, asset_id: str) -> list[dict]:
        self.get_asset(asset_id)
        return [i.to_dict() for i in self.operational_history.get(asset_id, [])]

    # ================================================================== validation & invariants
    def validate_network(self) -> dict:
        errors: list[str] = []
        warnings: list[str] = []
        for a in self.assets.values():
            if isinstance(a, Producer) and "out" not in a.outputs:
                warnings.append(f"producer '{a.id}' output is not connected")
            if isinstance(a, Transformer):
                r = a.recipe
                if r is None:
                    warnings.append(f"transformer '{a.id}' has no recipe")
                    continue
                for i in range(len(r.inputs)):
                    if f"in{i}" not in a.inputs:
                        warnings.append(f"transformer '{a.id}' input slot in{i} is not connected")
                for j in range(len(r.outputs)):
                    if f"out{j}" not in a.outputs:
                        warnings.append(f"transformer '{a.id}' output slot out{j} is not connected")
            if isinstance(a, Splitter):
                for port in ("in", "out_a", "out_b"):
                    if port not in a.inputs and port not in a.outputs:
                        warnings.append(f"splitter '{a.id}' port {port} is not connected")
            if isinstance(a, Conveyor):
                src_type = self._static_output_type(a.source)
                dst = self.assets.get(a.target[0])
                if src_type and isinstance(dst, Transformer) and dst.recipe:
                    slot = int(a.target[1][2:])
                    if slot < len(dst.recipe.inputs) and dst.recipe.inputs[slot].product_type_id != src_type:
                        errors.append(f"conveyor '{a.id}' delivers '{src_type}' but {dst.id}.{a.target[1]} "
                                      f"expects '{dst.recipe.inputs[slot].product_type_id}'")
                if src_type and isinstance(dst, Sink) and dst.config["accepted_product_type_ids"] is not None \
                        and src_type not in dst.config["accepted_product_type_ids"]:
                    errors.append(f"conveyor '{a.id}' delivers '{src_type}' that sink '{dst.id}' rejects")
        return {"valid": not errors, "errors": errors, "warnings": warnings}

    def _static_output_type(self, source: tuple[str, str]) -> str | None:
        a = self.assets.get(source[0])
        if isinstance(a, Producer):
            return a.config["product_type_id"]
        if isinstance(a, Transformer) and a.recipe:
            slot = int(source[1][3:])
            return a.recipe.outputs[slot].product_type_id if slot < len(a.recipe.outputs) else None
        return None

    def check_invariants(self) -> list[str]:
        """Return a list of invariant violations (empty when the state is consistent)."""
        violations: list[str] = []
        held: dict[str, str] = {}
        for asset in self.assets.values():
            for pid, area in asset.contained():
                if pid in held:
                    violations.append(f"product {pid} held by both {held[pid]} and {asset.id}")
                held[pid] = asset.id
                p = self.products.get(pid)
                if p is None:
                    violations.append(f"unknown product {pid} in {asset.id}")
                    continue
                if not p.is_active:
                    violations.append(f"terminal product {pid} ({p.status.value}) still held by {asset.id}")
                if p.location is None or p.location.asset_id != asset.id or p.location.area != area:
                    violations.append(f"product {pid} location {p.location} does not match {asset.id}/{area}")
                expected = {"conveyor": ProductStatus.IN_TRANSIT, "cycle": ProductStatus.RESERVED}.get(
                    area, ProductStatus.QUEUED)
                if p.status != expected:
                    violations.append(f"product {pid} in {area} has status {p.status.value}")
        if set(held) != self.active_product_ids:
            lost = self.active_product_ids - set(held)
            extra = set(held) - self.active_product_ids
            if lost:
                violations.append(f"{len(lost)} active product(s) not held by any asset (lost): {sorted(lost)[:5]}")
            if extra:
                violations.append(f"{len(extra)} held product(s) not registered active: {sorted(extra)[:5]}")
        for pid in self.active_product_ids:
            p = self.products[pid]
            if p.status == ProductStatus.RESERVED:
                c = next((c for c in self.cycles.values() if c.status == "active" and pid in c.input_ids), None)
                if c is None:
                    violations.append(f"reserved product {pid} does not belong to an active cycle")
        terminal = sum(self.counters[s] for s in ("consumed", "completed", "stored"))
        if self.counters["created"] != len(self.active_product_ids) + terminal:
            violations.append(f"conservation violated: created={self.counters['created']} "
                              f"active={len(self.active_product_ids)} terminal={terminal}")
        if len(self.products) != self.counters["created"]:
            violations.append("product registry size differs from created count")
        for c in self.cycles.values():
            if c.kind == "transformation" and c.status == "completed":
                recipe = c.config_snapshot["recipe"]
                need_in = sum(i["quantity"] for i in recipe["inputs"])
                need_out = sum(o["quantity"] for o in recipe["outputs"])
                if len(c.input_ids) != need_in or len(c.output_ids) != need_out:
                    violations.append(f"cycle {c.id} consumed {len(c.input_ids)}/{need_in} and produced "
                                      f"{len(c.output_ids)}/{need_out}")
                for pid in c.input_ids:
                    if self.products[pid].status != ProductStatus.CONSUMED:
                        violations.append(f"cycle {c.id} input {pid} not consumed")
        for s in (a for a in self.assets.values() if isinstance(a, Splitter)):
            if s.routed_totals["A"] + s.routed_totals["B"] != sum(
                    1 for p in self.products.values() for e in p.history if e.activity == s.id):
                violations.append(f"splitter {s.id} routing count mismatch")
        return violations

    # ================================================================== views
    def metrics(self) -> dict:
        status_counts = Counter(self.products[pid].status.value for pid in self.active_product_ids)
        return {
            "sim_time": self.now, "events_processed": self.events_processed,
            "scheduled_events": len(self.scheduler),
            "products_created": self.counters["created"], "products_active": len(self.active_product_ids),
            "products_consumed": self.counters["consumed"], "products_completed": self.counters["completed"],
            "products_stored": self.counters["stored"], "active_by_status": dict(status_counts),
            "cycles_total": len(self.cycles), "machine_events": self._event_seq,
            "listener_errors": self.listener_errors,
        }

    def snapshot(self) -> dict:
        return {"name": self.name, "seed": self.seed, "time": self.now, "started": self.started,
                "assets": [a.snapshot() for a in self.assets.values()], "metrics": self.metrics(),
                "last_event_seq": self._event_seq, "next_event_time": self.next_event_time()}

    def pending_operations(self) -> dict:
        active_cycles = [c.to_dict() for c in self.cycles.values() if c.status == "active"]
        upcoming = [{"time": e.time, "kind": e.kind, "asset_id": e.target}
                    for e in self.scheduler.pending()[:100] if e.kind != "wake"]
        return {"active_cycles": active_cycles, "scheduled_events": upcoming}

    def iter_assets(self, kind: type | None = None) -> Iterable[Asset]:
        return (a for a in self.assets.values() if kind is None or isinstance(a, kind))
