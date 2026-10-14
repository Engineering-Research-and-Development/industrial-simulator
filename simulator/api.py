"""HTTP API (FastAPI) and static hosting of the 2D editor.

All endpoints operate on a single in-process simulation guarded by the controller lock.
Simulation rules live exclusively in the engine; the API only validates request shapes and
translates engine errors into HTTP status codes.
"""

from __future__ import annotations

import itertools
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .controller import SimulationController
from .engine import Simulation
from .errors import NotFoundError, SimulatorError, ValidationError
from .integration import IntegrationBridge, create_publisher
from .scenario import (export_scenario, list_builtin_scenarios, list_checkpoints, load_checkpoint, load_scenario,
                       read_builtin_scenario, save_checkpoint)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class SimulationService:
    """Owns the controller, the active simulation and the optional broker bridge."""

    def __init__(self, sim: Simulation | None = None, *, checkpoint_dir: Path | None = None,
                 background: bool = True) -> None:
        self.controller = SimulationController(sim or Simulation(seed=42))
        self.bridge: IntegrationBridge | None = None
        self.checkpoint_dir = checkpoint_dir
        self.background = background
        self._metric_seq = itertools.count(1)
        self._last_metrics_wall = 0.0
        self.controller.on_tick.append(self._publish_metrics)

    @property
    def sim(self) -> Simulation:
        return self.controller.sim

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


def _int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"'{name}' must be an integer") from None


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

    # ------------------------------------------------------------------ assets
    @app.get("/api/assets")
    def list_assets():
        with lock:
            return [a.snapshot() for a in svc.sim.assets.values()]

    @app.post("/api/assets", status_code=201)
    def create_asset(payload: Any = Body(...)):
        d = body_dict(payload)
        with lock:
            if d.get("kind") == "conveyor":
                a = svc.sim.add_conveyor(d.get("name", ""), d.get("source"), d.get("target"), d.get("config") or {},
                                         asset_id=d.get("id"), position=d.get("position"))
            else:
                a = svc.sim.add_asset(d.get("kind"), d.get("name", ""), d.get("config") or {},
                                      asset_id=d.get("id"), position=d.get("position"))
            return a.snapshot()

    @app.post("/api/connections", status_code=201)
    def connect(payload: Any = Body(...)):
        d = body_dict(payload)
        with lock:
            return svc.sim.add_conveyor(d.get("name") or "Conveyor", d.get("source"), d.get("target"),
                                        d.get("config") or {}, asset_id=d.get("id")).snapshot()

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
            return {"ok": True, "name": sim.name, "assets": len(sim.assets)}

    @app.get("/api/scenarios")
    def scenarios():
        return list_builtin_scenarios()

    @app.post("/api/scenarios/{scenario_id}/load")
    def load_builtin(scenario_id: str):
        with lock:
            sim = load_scenario(read_builtin_scenario(scenario_id))
            svc.install_simulation(sim, "idle")
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
