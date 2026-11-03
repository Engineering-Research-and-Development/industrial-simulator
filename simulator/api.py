"""HTTP API (FastAPI) and static hosting of the 2D editor.

All endpoints operate on a single in-process simulation guarded by the controller lock.
Simulation rules live exclusively in the engine; the API only validates request shapes and
translates engine errors into HTTP status codes.
"""

from __future__ import annotations

import itertools
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .controller import SimulationController
from .engine import Simulation
from .errors import ConflictError, NotFoundError, SimulatorError, ValidationError
from .industrial.asset_types import TYPE_KINDS
from .industrial.datasets import DATASETS
from .industrial.ingestion import ingest
from .industrial.mqtt import MqttIngestor
from .industrial.units import known_units
from .integration import IntegrationBridge, create_publisher
from .scenario import (export_scenario, list_builtin_scenarios, list_checkpoints, load_checkpoint, load_scenario,
                       new_scenario, read_builtin_scenario, rename_scenario_file, save_checkpoint, scenario_exists,
                       write_scenario)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class SimulationService:
    """Owns the controller, the active simulation and the optional broker bridge."""

    def __init__(self, sim: Simulation | None = None, *, checkpoint_dir: Path | None = None,
                 background: bool = True, mqtt_client_factory=None, scenario_dir: Path | None = None,
                 scenario_id: str | None = None) -> None:
        self.controller = SimulationController(sim or Simulation(seed=42))
        self.sim.industrial.ensure_default_types()
        self.bridge: IntegrationBridge | None = None
        self.checkpoint_dir = checkpoint_dir
        self.scenario_dir = scenario_dir
        self.scenario_id = scenario_id  # scenario file backing the current plant (None = unsaved)
        self.background = background
        self._metric_seq = itertools.count(1)
        self._last_metrics_wall = 0.0
        self.controller.on_tick.append(self._publish_metrics)
        self.mqtt = MqttIngestor(self.controller.lock, lambda: self.sim, client_factory=mqtt_client_factory,
                                 before_ingest=self.sync_clock)

    @property
    def sim(self) -> Simulation:
        return self.controller.sim

    def sync_clock(self) -> None:
        """Advance a running simulation to the current wall-clock target before applying external events."""
        if self.controller.status == "running":
            self.controller.tick()

    def ingest(self, envelope: Any, transport: str = "rest") -> dict:
        with self.controller.lock:
            self.sync_clock()
            return ingest(self.sim, envelope, transport=transport)

    def _publish_metrics(self) -> None:
        if self.bridge is None:
            return
        now = self.controller.time_fn()
        if now - self._last_metrics_wall >= 1.0:
            self._last_metrics_wall = now
            self.bridge.publish_metrics(self.sim.metrics(), f"MET-{id(self.sim):x}-{next(self._metric_seq)}")

    def install_simulation(self, sim: Simulation, status: str = "idle") -> None:
        if self.bridge is not None:
            if self.bridge in self.sim.listeners:
                self.sim.listeners.remove(self.bridge)
            sim.listeners.append(self.bridge)
        self.controller.replace_simulation(sim, status)
        with self.controller.lock:
            sim.industrial.ensure_default_types()

    def enable_integration(self, adapter: str, options: dict | None, topic_prefix: str,
                           categories: list[str] | None) -> dict:
        self.disable_integration()
        publisher = create_publisher(adapter, options)
        self.bridge = IntegrationBridge(publisher, topic_prefix=topic_prefix, categories=categories)
        self.sim.listeners.append(self.bridge)
        if self.background:
            self.bridge.start()
        return self.bridge.info()

    def disable_integration(self) -> None:
        if self.bridge is not None:
            if self.bridge in self.sim.listeners:
                self.sim.listeners.remove(self.bridge)
            self.bridge.stop()
            self.bridge = None

    def shutdown(self) -> None:
        self.controller.stop_background()
        self.disable_integration()
        self.mqtt.stop()


def _int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"'{name}' must be an integer") from None


def slug_id(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("scenario name must be a non-empty string")
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")[:64]
    return s or "scenario"


def create_app(service: SimulationService | None = None) -> FastAPI:
    svc = service or SimulationService()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if svc.background:
            svc.controller.start_background()
        yield
        svc.shutdown()

    app = FastAPI(title="Industrial Digital Twin Simulator", version="0.1.0", lifespan=lifespan)
    app.state.service = svc
    lock = svc.controller.lock

    @app.exception_handler(SimulatorError)
    async def _sim_error(_: Request, exc: SimulatorError):
        return JSONResponse(status_code=exc.status_code, content={"error": type(exc).__name__, "detail": str(exc)})

    def body_dict(payload: Any) -> dict:
        if not isinstance(payload, dict):
            raise ValidationError("request body must be a JSON object")
        return payload

    # ------------------------------------------------------------------ simulation control
    @app.get("/api/sim")
    def sim_info():
        return svc.controller.info()

    @app.post("/api/sim/start")
    def sim_start():
        with lock:
            validation = svc.sim.validate_network()
            if not validation["valid"]:
                raise ValidationError("network validation failed: " + "; ".join(validation["errors"]))
            svc.controller.start()
            return {**svc.controller.info(), "validation": validation}

    @app.post("/api/sim/pause")
    def sim_pause():
        svc.controller.pause()
        return svc.controller.info()

    @app.post("/api/sim/resume")
    def sim_resume():
        svc.controller.resume()
        return svc.controller.info()

    @app.post("/api/sim/stop")
    def sim_stop():
        svc.controller.stop()
        return svc.controller.info()

    @app.post("/api/sim/reset")
    def sim_reset():
        with lock:
            fresh = load_scenario(export_scenario(svc.sim))
            svc.install_simulation(fresh, "idle")
            return svc.controller.info()

    @app.post("/api/sim/acceleration")
    def sim_acceleration(payload: Any = Body(...)):
        svc.controller.set_acceleration(body_dict(payload).get("factor"))
        return svc.controller.info()

    @app.post("/api/sim/step")
    def sim_step(payload: Any = Body(...)):
        svc.controller.step(body_dict(payload).get("seconds"))
        return svc.controller.info()

    @app.get("/api/state")
    def state():
        with lock:
            return {"sim": svc.controller.info(), **svc.sim.snapshot()}

    @app.get("/api/validation")
    def validation():
        with lock:
            return svc.sim.validate_network()

    @app.get("/api/invariants")
    def invariants():
        with lock:
            v = svc.sim.check_invariants()
            return {"ok": not v, "violations": v}

    @app.get("/api/metrics")
    def metrics():
        with lock:
            return svc.sim.metrics()

    @app.get("/api/pending")
    def pending():
        with lock:
            return svc.sim.pending_operations()

    # ------------------------------------------------------------------ product types / recipes / orders
    @app.get("/api/product-types")
    def list_types():
        with lock:
            return [p.to_dict() for p in svc.sim.product_types.values()]

    @app.post("/api/product-types", status_code=201)
    def create_type(payload: Any = Body(...)):
        with lock:
            return svc.sim.add_product_type(body_dict(payload)).to_dict()

    @app.put("/api/product-types/{type_id}")
    def update_type(type_id: str, payload: Any = Body(...)):
        with lock:
            return svc.sim.update_product_type(type_id, body_dict(payload)).to_dict()

    @app.delete("/api/product-types/{type_id}", status_code=204)
    def delete_type(type_id: str):
        with lock:
            svc.sim.delete_product_type(type_id)

    @app.get("/api/recipes")
    def list_recipes():
        with lock:
            return [r.to_dict() for r in svc.sim.recipes.values()]

    @app.post("/api/recipes", status_code=201)
    def create_recipe(payload: Any = Body(...)):
        with lock:
            return svc.sim.add_recipe(body_dict(payload)).to_dict()

    @app.put("/api/recipes/{recipe_id}")
    def update_recipe(recipe_id: str, payload: Any = Body(...)):
        with lock:
            return svc.sim.update_recipe(recipe_id, body_dict(payload)).to_dict()

    @app.delete("/api/recipes/{recipe_id}", status_code=204)
    def delete_recipe(recipe_id: str):
        with lock:
            svc.sim.delete_recipe(recipe_id)

    @app.get("/api/orders")
    def list_orders():
        with lock:
            return [o.to_dict() for o in svc.sim.orders.values()]

    @app.post("/api/orders", status_code=201)
    def create_order(payload: Any = Body(...)):
        with lock:
            return svc.sim.add_order(body_dict(payload)).to_dict()

    @app.put("/api/orders/{order_id}")
    def update_order(order_id: str, payload: Any = Body(...)):
        with lock:
            return svc.sim.update_order(order_id, body_dict(payload)).to_dict()

    @app.delete("/api/orders/{order_id}", status_code=204)
    def delete_order(order_id: str):
        with lock:
            svc.sim.delete_order(order_id)

    # ------------------------------------------------------------------ assets
    @app.get("/api/assets")
    def list_assets():
        with lock:
            return [a.snapshot() for a in svc.sim.assets.values()]

    @app.post("/api/assets", status_code=201)
    def create_asset(payload: Any = Body(...)):
        d = body_dict(payload)
        with lock:
            return create_typed_asset(d.get("kind"), d.get("name", ""), d).snapshot()

    @app.post("/api/connections", status_code=201)
    def connect(payload: Any = Body(...)):
        d = body_dict(payload)
        with lock:
            return create_typed_asset("conveyor", d.get("name") or "Conveyor", {**d, "position": None}).snapshot()

    def create_typed_asset(kind: Any, name: str, d: dict):
        """Create an asset bound to ``type_id`` – by default the per-kind default asset type.

        ``"type_id": null`` creates an unbound asset. Instance config overrides the type template."""
        hub = svc.sim.industrial
        type_id = d.get("type_id", hub.default_type_id(kind) if isinstance(kind, str) else None)
        if type_id is None or kind not in TYPE_KINDS:
            if kind == "conveyor":
                return svc.sim.add_conveyor(name, d.get("source"), d.get("target"), d.get("config") or {},
                                            asset_id=d.get("id"), position=d.get("position"))
            return svc.sim.add_asset(kind, name, d.get("config") or {}, asset_id=d.get("id"), position=d.get("position"))
        if type_id == hub.default_type_id(kind):
            hub.ensure_default_types()
        t = hub.get_type(type_id)
        if t.kind != kind:
            raise ValidationError(f"asset type '{type_id}' is for {t.kind}s, not {kind}s")
        return hub.deploy(type_id, name, config=d.get("config") or {}, asset_id=d.get("id"),
                          position=d.get("position"), source=d.get("source"), target=d.get("target"))

    @app.get("/api/assets/{asset_id}")
    def get_asset(asset_id: str):
        with lock:
            a = svc.sim.get_asset(asset_id)
            return {**a.snapshot(), "stats": a.stats()}

    @app.delete("/api/assets/{asset_id}", status_code=204)
    def delete_asset(asset_id: str):
        with lock:
            svc.sim.remove_asset(asset_id)

    @app.patch("/api/assets/{asset_id}/config")
    def patch_config(asset_id: str, payload: Any = Body(...)):
        with lock:
            return svc.sim.update_config(asset_id, body_dict(payload))

    @app.post("/api/assets/{asset_id}/rename")
    def rename(asset_id: str, payload: Any = Body(...)):
        with lock:
            return svc.sim.rename_asset(asset_id, body_dict(payload).get("name")).snapshot()

    @app.put("/api/assets/{asset_id}/position")
    def position(asset_id: str, payload: Any = Body(...)):
        with lock:
            svc.sim.set_position(asset_id, body_dict(payload))
            return {"ok": True}

    @app.post("/api/assets/{asset_id}/commands")
    def command(asset_id: str, payload: Any = Body(...)):
        with lock:
            svc.sim.command(asset_id, body_dict(payload).get("command"))
            return svc.sim.get_asset(asset_id).snapshot()

    @app.get("/api/assets/{asset_id}/stats")
    def asset_stats(asset_id: str):
        with lock:
            return svc.sim.get_asset(asset_id).stats()

    @app.get("/api/assets/{asset_id}/operational-history")
    def op_history(asset_id: str, limit: int = Query(500, ge=1, le=10_000)):
        with lock:
            return svc.sim.operational_history_for(asset_id)[-limit:]

    @app.get("/api/assets/{asset_id}/products")
    def sink_products(asset_id: str, completed: bool | None = None, defective: bool | None = None,
                      product_type_id: str | None = None, lot_number: str | None = None,
                      order_id: str | None = None, cycle_id: str | None = None,
                      offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=5000)):
        with lock:
            a = svc.sim.get_asset(asset_id)
            if a.kind != "sink":
                raise ValidationError("product listing by asset is only available for sinks; use /api/products")
            return [p.summary() for p in a.products(completed=completed, defective=defective,  # type: ignore
                                                    product_type_id=product_type_id, lot_number=lot_number,
                                                    order_id=order_id, cycle_id=cycle_id, offset=offset,
                                                    limit=limit)]

    @app.get("/api/config-changes")
    def config_changes(asset_id: str | None = None, limit: int = Query(200, ge=1, le=10_000)):
        with lock:
            items = [c.to_dict() for c in svc.sim.config_changes if asset_id is None or c.asset_id == asset_id]
            return items[-limit:]

    # ------------------------------------------------------------------ products, cycles, events
    @app.get("/api/products")
    def products(status: str | None = None, product_type_id: str | None = None, asset_id: str | None = None,
                 order_id: str | None = None, lot_number: str | None = None, defective: bool | None = None,
                 offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=5000)):
        with lock:
            return [p.summary() for p in svc.sim.query_products(
                status=status, product_type_id=product_type_id, asset_id=asset_id, order_id=order_id,
                lot_number=lot_number, defective=defective, offset=offset, limit=limit)]

    @app.get("/api/products/{product_id}")
    def product(product_id: str):
        with lock:
            return svc.sim.product_record(product_id)

    @app.get("/api/products/{product_id}/history")
    def product_history(product_id: str, include_inherited: bool = True):
        with lock:
            if include_inherited:
                return svc.sim.full_history(product_id)
            return [e.to_dict() for e in svc.sim.get_product(product_id).history]

    @app.get("/api/cycles")
    def cycles(asset_id: str | None = None, status: str | None = None, limit: int = Query(100, ge=1, le=5000)):
        with lock:
            items = [c for c in svc.sim.cycles.values()
                     if (asset_id is None or c.asset_id == asset_id) and (status is None or c.status == status)]
            return [c.to_dict() for c in items[-limit:]]

    @app.get("/api/cycles/{cycle_id}")
    def cycle(cycle_id: str):
        with lock:
            c = svc.sim.cycles.get(cycle_id)
            if c is None:
                raise NotFoundError(f"unknown cycle '{cycle_id}'")
            return {**c.to_dict(), "events": [e.to_dict() for e in svc.sim.query_events(cycle_id=cycle_id)]}

    @app.get("/api/events")
    def events(asset_id: str | None = None, event_type: str | None = None, cycle_id: str | None = None,
               product_id: str | None = None, order_id: str | None = None, since_seq: int | None = None,
               limit: int = Query(200, ge=1, le=10_000)):
        with lock:
            return [e.to_dict() for e in svc.sim.query_events(
                asset_id=asset_id, event_type=event_type, cycle_id=cycle_id, product_id=product_id,
                order_id=order_id, since_seq=since_seq, limit=limit)]

    # ------------------------------------------------------------------ scenarios & checkpoints
    @app.get("/api/scenario")
    def get_scenario():
        with lock:
            return export_scenario(svc.sim)

    @app.post("/api/scenario")
    def put_scenario(payload: Any = Body(...)):
        with lock:
            sim = load_scenario(body_dict(payload))
            svc.install_simulation(sim, "idle")
            svc.scenario_id = None
            return {"ok": True, "name": sim.name, "assets": len(sim.assets)}

    @app.get("/api/scenarios")
    def scenarios():
        return list_builtin_scenarios(svc.scenario_dir)

    def current_info() -> dict:
        return {"id": svc.scenario_id, "name": svc.sim.name, "description": getattr(svc.sim, "description", ""),
                "saved": svc.scenario_id is not None and scenario_exists(svc.scenario_id, svc.scenario_dir)}

    @app.get("/api/scenarios/current")
    def scenario_current():
        with lock:
            return current_info()

    @app.post("/api/scenarios", status_code=201)
    def scenario_create(payload: Any = Body(...)):
        """New empty plant: keeps the catalogues (product types, recipes, orders, asset types), writes a new file."""
        d = body_dict(payload)
        name = d.get("name")
        sid = d.get("id") or slug_id(name)
        seed = d.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ValidationError("seed must be an integer or null")
        with lock:
            if scenario_exists(sid, svc.scenario_dir):
                raise ConflictError(f"scenario '{sid}' already exists")
            sim = new_scenario(svc.sim, name=name, description=d.get("description", ""), seed=seed,
                               keep_catalog=bool(d.get("keep_catalog", True)),
                               keep_asset_types=bool(d.get("keep_asset_types", True)))
            write_scenario(export_scenario(sim), sid, svc.scenario_dir, overwrite=False)
            svc.install_simulation(sim, "idle")
            svc.scenario_id = sid
            return current_info()

    @app.put("/api/scenarios/{scenario_id}")
    def scenario_save(scenario_id: str, payload: Any = Body(None), overwrite: bool = False):
        """Save the current plant to ``scenario_id`` (Save when it is the current file, Save As otherwise)."""
        d = body_dict(payload) if payload is not None else {}
        with lock:
            if scenario_id != svc.scenario_id and scenario_exists(scenario_id, svc.scenario_dir) and not overwrite:
                raise ConflictError(f"scenario '{scenario_id}' already exists (use overwrite=true)")
            if d.get("name") is not None:
                if not isinstance(d["name"], str) or not d["name"].strip():
                    raise ValidationError("scenario name must be a non-empty string")
                svc.sim.name = d["name"].strip()
            if d.get("description") is not None:
                svc.sim.description = str(d["description"])
            write_scenario(export_scenario(svc.sim), scenario_id, svc.scenario_dir)
            svc.scenario_id = scenario_id
            return current_info()

    @app.post("/api/scenarios/{scenario_id}/rename")
    def scenario_rename(scenario_id: str, payload: Any = Body(...)):
        """Rename a scenario file (``new_id``) and/or its display name (``name``)."""
        d = body_dict(payload)
        new_id, name = d.get("new_id") or scenario_id, d.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValidationError("scenario name must be a non-empty string")
        with lock:
            doc = read_builtin_scenario(scenario_id, svc.scenario_dir)
            rename_scenario_file(scenario_id, new_id, svc.scenario_dir)
            if name is not None:
                doc["name"] = name.strip()
                write_scenario(doc, new_id, svc.scenario_dir)
            if svc.scenario_id == scenario_id:
                svc.scenario_id = new_id
                if name is not None:
                    svc.sim.name = name.strip()
            return {"id": new_id, "name": doc.get("name", new_id), "current": current_info()}

    @app.post("/api/scenarios/{scenario_id}/load")
    def load_builtin(scenario_id: str):
        with lock:
            sim = load_scenario(read_builtin_scenario(scenario_id, svc.scenario_dir))
            svc.install_simulation(sim, "idle")
            svc.scenario_id = scenario_id
            return {"ok": True, "name": sim.name, "assets": len(sim.assets)}

    @app.get("/api/checkpoints")
    def checkpoints():
        return list_checkpoints(svc.checkpoint_dir)

    @app.post("/api/checkpoints/{name}")
    def checkpoint_save(name: str):
        with lock:
            was_running = svc.controller.status == "running"
            if was_running:
                svc.controller.tick()
            save_checkpoint(svc.sim, name, svc.checkpoint_dir)
            return {"ok": True, "name": name, "sim_time": svc.sim.now}

    @app.post("/api/checkpoints/{name}/restore")
    def checkpoint_restore(name: str):
        with lock:
            sim = load_checkpoint(name, svc.checkpoint_dir)
            svc.install_simulation(sim, "paused" if sim.started else "idle")
            return {"ok": True, "sim_time": sim.now, "status": svc.controller.status}

    # ------------------------------------------------------------------ integration
    @app.get("/api/integration")
    def integration_info():
        return svc.bridge.info() if svc.bridge else {"adapter": None}

    @app.post("/api/integration")
    def integration_enable(payload: Any = Body(...)):
        d = body_dict(payload)
        with lock:
            adapter = d.get("adapter")
            if adapter in (None, "none"):
                svc.disable_integration()
                return {"adapter": None}
            return svc.enable_integration(adapter, d.get("options"), d.get("topic_prefix", "industrial-sim"),
                                          d.get("categories"))

    @app.get("/api/integration/messages")
    def integration_messages(limit: int = Query(50, ge=1, le=1000)):
        bridge = svc.bridge
        if bridge is None or not hasattr(bridge.publisher, "decoded"):
            raise ValidationError("message inspection is only available for the in-memory adapter")
        return bridge.publisher.decoded()[-limit:]  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ asset types
    def hub():
        return svc.sim.industrial

    @app.get("/api/asset-types")
    def asset_types():
        with lock:
            h = hub()
            return [h.type_summary(t) for t in h.asset_types.values()]

    @app.post("/api/asset-types", status_code=201)
    def asset_type_create(payload: Any = Body(...)):
        with lock:
            h = hub()
            return h.type_summary(h.create_type(body_dict(payload)))

    @app.get("/api/asset-types/{type_id}")
    def asset_type_get(type_id: str):
        with lock:
            h = hub()
            return h.type_summary(h.get_type(type_id))

    @app.put("/api/asset-types/{type_id}")
    def asset_type_update(type_id: str, payload: Any = Body(...)):
        with lock:
            h = hub()
            return h.type_summary(h.update_type(type_id, body_dict(payload)))

    @app.delete("/api/asset-types/{type_id}", status_code=204)
    def asset_type_delete(type_id: str):
        with lock:
            hub().delete_type(type_id)

    @app.post("/api/asset-types/{type_id}/duplicate", status_code=201)
    def asset_type_duplicate(type_id: str, payload: Any = Body(...)):
        d = body_dict(payload)
        with lock:
            h = hub()
            return h.type_summary(h.duplicate_type(type_id, d.get("id"), d.get("name")))

    @app.post("/api/asset-types/{type_id}/deploy", status_code=201)
    def asset_type_deploy(type_id: str, payload: Any = Body(...)):
        d = body_dict(payload)
        with lock:
            a = hub().deploy(type_id, d.get("name", ""), config=d.get("config"), asset_id=d.get("id"),
                             position=d.get("position"), overrides=d.get("overrides"), local=d.get("local"),
                             source=d.get("source"), target=d.get("target"))
            return {**a.snapshot(), "industrial": hub().describe(a.id)}

    @app.post("/api/asset-types/{type_id}/sync")
    def asset_type_sync(type_id: str, dry_run: bool = False):
        with lock:
            return hub().sync_type_instances(type_id, dry_run=dry_run)

    # ------------------------------------------------------------------ per-asset industrial profile
    @app.get("/api/assets/{asset_id}/industrial")
    def asset_industrial(asset_id: str):
        with lock:
            return hub().describe(asset_id)

    @app.put("/api/assets/{asset_id}/parameters")
    def asset_parameters(asset_id: str, payload: Any = Body(...)):
        with lock:
            return hub().set_parameters(asset_id, body_dict(payload))

    @app.put("/api/assets/{asset_id}/overrides/{section}/{key}")
    def asset_override_set(asset_id: str, section: str, key: str, payload: Any = Body(...)):
        with lock:
            return hub().set_item_override(asset_id, section, key, body_dict(payload))

    @app.delete("/api/assets/{asset_id}/overrides/{section}/{key}")
    def asset_override_reset(asset_id: str, section: str, key: str):
        with lock:
            return hub().reset_override(asset_id, section, key)

    @app.put("/api/assets/{asset_id}/local")
    def asset_local(asset_id: str, payload: Any = Body(...)):
        with lock:
            return hub().set_local(asset_id, body_dict(payload))

    @app.post("/api/assets/{asset_id}/bind")
    def asset_bind(asset_id: str, payload: Any = Body(...)):
        d = body_dict(payload)
        with lock:
            return hub().attach_type(asset_id, d.get("type_id"), apply_config=bool(d.get("apply_config", True)))

    @app.post("/api/assets/{asset_id}/unbind")
    def asset_unbind(asset_id: str):
        with lock:
            return hub().detach_type(asset_id)

    @app.post("/api/assets/{asset_id}/sync-type")
    def asset_sync_type(asset_id: str, dry_run: bool = False):
        with lock:
            return hub().sync_instance(asset_id, dry_run=dry_run)

    @app.get("/api/assets/{asset_id}/sync")
    def asset_sync_get(asset_id: str):
        with lock:
            svc.sim.get_asset(asset_id)
            return hub().sync_config(asset_id)

    @app.put("/api/assets/{asset_id}/sync")
    def asset_sync_set(asset_id: str, payload: Any = Body(...)):
        with lock:
            return hub().set_sync(asset_id, body_dict(payload))

    @app.get("/api/assets/{asset_id}/measurements")
    def asset_measurements(asset_id: str, measurement_id: str | None = None, since_seq: int | None = None,
                           since_time: float | None = None, cycle_id: str | None = None,
                           limit: int = Query(500, ge=1, le=20_000)):
        with lock:
            svc.sim.get_asset(asset_id)
            store = hub().store
            return {"latest": {m: o.to_dict() for m, o in store.latest_for(asset_id).items()},
                    "observations": [o.to_dict() for o in store.query(
                        asset_id=asset_id, measurement_id=measurement_id, since_seq=since_seq,
                        since_time=since_time, cycle_id=cycle_id, limit=limit)]}

    @app.get("/api/assets/{asset_id}/conditions")
    def asset_conditions(asset_id: str, limit: int = Query(200, ge=1, le=10_000)):
        with lock:
            svc.sim.get_asset(asset_id)
            h = hub()
            return {"active": h.active_conditions(asset_id), "classification": h.classification(asset_id),
                    "blocking": h.blocking_condition(asset_id), "log": h.condition_log(asset_id=asset_id, limit=limit)}

    @app.get("/api/conditions/log")
    def conditions_log(limit: int = Query(200, ge=1, le=10_000)):
        with lock:
            return hub().condition_log(limit=limit)

    @app.post("/api/expressions/validate")
    def expression_validate(payload: Any = Body(...)):
        d = body_dict(payload)
        with lock:
            return hub().check_expression(d.get("expression"), asset_id=d.get("asset_id"),
                                          type_id=d.get("type_id"), kind=d.get("kind"))

    @app.get("/api/units")
    def units():
        return known_units()

    # ------------------------------------------------------------------ external ingestion
    @app.post("/api/ingestion/events")
    def ingestion_events(payload: Any = Body(...)):
        if isinstance(payload, list):
            if len(payload) > 1000:
                raise ValidationError("at most 1000 envelopes per request")
            return {"results": [svc.ingest(e) for e in payload]}
        return svc.ingest(payload)

    @app.get("/api/ingestion/status")
    def ingestion_status():
        with lock:
            return {**hub().ingestion.status(), "mqtt": svc.mqtt.info()}

    @app.get("/api/ingestion/messages")
    def ingestion_messages(limit: int = Query(100, ge=1, le=500), status: str | None = None):
        with lock:
            msgs = [m for m in hub().ingestion.messages if status is None or m.get("status") == status]
            return msgs[-limit:]

    @app.get("/api/ingestion/mqtt")
    def mqtt_info():
        return svc.mqtt.info()

    @app.put("/api/ingestion/mqtt")
    def mqtt_configure(payload: Any = Body(...)):
        svc.mqtt.configure(body_dict(payload))
        return svc.mqtt.info()

    @app.post("/api/ingestion/mqtt/start")
    def mqtt_start():
        return svc.mqtt.start()

    @app.post("/api/ingestion/mqtt/stop")
    def mqtt_stop():
        svc.mqtt.stop()
        return svc.mqtt.info()

    # ------------------------------------------------------------------ datasets
    def ds():
        return svc.sim.datasets

    def _dataset_name(name: str) -> str:
        if name not in DATASETS:
            raise ValidationError(f"dataset must be one of {list(DATASETS)}")
        return name

    @app.get("/api/datasets")
    def datasets_summary():
        with lock:
            return ds().summary()

    @app.post("/api/datasets/sessions", status_code=201)
    def dataset_create(payload: Any = Body(...)):
        with lock:
            return ds().create_session(body_dict(payload)).summary()

    @app.get("/api/datasets/sessions/{sid}")
    def dataset_get(sid: str):
        with lock:
            return ds().get(sid).summary()

    @app.put("/api/datasets/sessions/{sid}")
    def dataset_update(sid: str, payload: Any = Body(...)):
        with lock:
            return ds().update_session(sid, body_dict(payload)).summary()

    @app.delete("/api/datasets/sessions/{sid}", status_code=204)
    def dataset_delete(sid: str):
        with lock:
            ds().delete_session(sid)

    @app.post("/api/datasets/sessions/{sid}/{action}")
    def dataset_action(sid: str, action: str):
        if action not in ("start", "pause", "resume", "stop", "clear"):
            raise NotFoundError(f"unknown session action '{action}'")
        with lock:
            svc.sync_clock()
            return getattr(ds(), action)(sid).summary()

    @app.get("/api/datasets/sessions/{sid}/preview/{dataset}")
    def dataset_preview(sid: str, dataset: str, limit: int = Query(50, ge=0, le=1000)):
        with lock:
            return ds().preview(sid, _dataset_name(dataset), limit)

    @app.get("/api/datasets/sessions/{sid}/export/{dataset}")
    def dataset_export(sid: str, dataset: str, format: str | None = None):
        with lock:
            data, name, mime = ds().export(sid, _dataset_name(dataset), format)
        return Response(content=data, media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/datasets/sessions/{sid}/manifest")
    def dataset_manifest(sid: str):
        with lock:
            return ds().manifest(sid, list(ds().get(sid).config["datasets"]))

    @app.get("/api/datasets/sessions/{sid}/bundle")
    def dataset_bundle(sid: str, datasets: str | None = None, format: str | None = None):
        names = [d.strip() for d in datasets.split(",") if d.strip()] if datasets else None
        with lock:
            data, name = ds().bundle(sid, names, format)
        return Response(content=data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------------ UI
    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.middleware("http")
        async def no_cache_ui(request, call_next):
            response = await call_next(request)
            if request.url.path == "/" or request.url.path.startswith("/static"):
                response.headers["Cache-Control"] = "no-cache"
            return response

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(WEB_DIR / "index.html")

    return app
