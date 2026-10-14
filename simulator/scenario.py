"""Scenario loading/saving (configuration) and checkpoints (full runtime state).

* **Scenarios** are JSON documents describing product types, recipes, production orders,
  assets, conveyors, layout positions and the RNG seed. Loading a scenario creates a fresh
  simulation at t=0 (this is also how *reset* works).
* **Checkpoints** capture the complete runtime state – active products and locations,
  in-progress cycles and reservations, scheduled events, RNG state, histories and records –
  using Python ``pickle``. Restoring resumes exactly where the checkpoint was taken without
  duplicating products or re-executing scheduled operations. Checkpoints are only written to
  and read from the server-side ``checkpoints`` directory by name; never load checkpoint
  files from untrusted sources.
"""

from __future__ import annotations

import json
import pickle
import re
from pathlib import Path

from .assets import Conveyor
from .engine import DEFAULT_ORDER_ID, Simulation
from .errors import NotFoundError, ValidationError

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"
CHECKPOINT_DIR = Path(__file__).resolve().parent.parent / "checkpoints"
_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
SCENARIO_FORMAT = "industrial-sim-scenario/v1"


def export_scenario(sim: Simulation) -> dict:
    assets, conveyors = [], []
    for a in sim.assets.values():
        entry = {"id": a.id, "kind": a.kind, "name": a.name, "config": a.to_config_dict(),
                 "position": dict(a.position)}
        if isinstance(a, Conveyor):
            entry["source"] = {"asset_id": a.source[0], "port": a.source[1]}
            entry["target"] = {"asset_id": a.target[0], "port": a.target[1]}
            conveyors.append(entry)
        else:
            assets.append(entry)
    return {
        "format": SCENARIO_FORMAT,
        "name": sim.name,
        "seed": sim.seed,
        "product_types": [p.to_dict() for p in sim.product_types.values()],
        "recipes": [r.to_dict() for r in sim.recipes.values()],
        "production_orders": [o.to_dict() for o in sim.orders.values() if o.id != DEFAULT_ORDER_ID],
        "assets": assets + conveyors,
    }


def load_scenario(data: dict, *, seed: int | None = None, event_log_limit: int | None = None) -> Simulation:
    """Build a new simulation from a scenario document. Raises ValidationError atomically."""
    if not isinstance(data, dict):
        raise ValidationError("scenario must be a JSON object")
    fmt = data.get("format", SCENARIO_FORMAT)
    if fmt != SCENARIO_FORMAT:
        raise ValidationError(f"unsupported scenario format {fmt!r} (expected {SCENARIO_FORMAT!r})")
    for key in ("product_types", "production_orders", "recipes", "assets"):
        if not isinstance(data.get(key, []), list):
            raise ValidationError(f"'{key}' must be a list")
    s = data.get("seed") if seed is None else seed
    if s is not None and (isinstance(s, bool) or not isinstance(s, int)):
        raise ValidationError("seed must be an integer or null")
    sim = Simulation(s, event_log_limit=event_log_limit, name=str(data.get("name") or "untitled"))
    try:
        for pt in data.get("product_types", []):
            sim.add_product_type(pt)
        for o in data.get("production_orders", []):
            sim.add_order(o)
        for r in data.get("recipes", []):
            sim.add_recipe(r)
        items = data.get("assets", [])
        if not isinstance(items, list):
            raise ValidationError("assets must be a list")
        for a in items:
            if a.get("kind") != "conveyor":
                sim.add_asset(a.get("kind"), a.get("name", a.get("id", "")), a.get("config") or {},
                              asset_id=a.get("id"), position=a.get("position"))
        for a in items:
            if a.get("kind") == "conveyor":
                sim.add_conveyor(a.get("name", a.get("id", "")), a.get("source"), a.get("target"),
                                 a.get("config") or {}, asset_id=a.get("id"), position=a.get("position"))
    except ValidationError:
        raise
    except (AttributeError, TypeError, KeyError) as exc:
        raise ValidationError(f"malformed scenario: {exc}") from exc
    return sim


def list_builtin_scenarios() -> list[dict]:
    out = []
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            out.append({"id": path.stem, "name": doc.get("name", path.stem), "description": doc.get("description", "")})
        except (OSError, json.JSONDecodeError):
            continue
    return out


def read_builtin_scenario(scenario_id: str) -> dict:
    if not _NAME.match(scenario_id or ""):
        raise ValidationError("invalid scenario id")
    path = SCENARIO_DIR / f"{scenario_id}.json"
    if not path.is_file():
        raise NotFoundError(f"unknown scenario '{scenario_id}'")
    return json.loads(path.read_text(encoding="utf-8"))


def _checkpoint_path(name: str, directory: Path | None = None) -> Path:
    if not _NAME.match(name or ""):
        raise ValidationError("checkpoint name must be 1-64 chars of letters, digits, '_' or '-'")
    return (directory or CHECKPOINT_DIR) / f"{name}.ckpt"


def save_checkpoint(sim: Simulation, name: str, directory: Path | None = None) -> Path:
    path = _checkpoint_path(name, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(pickle.dumps(sim, protocol=pickle.HIGHEST_PROTOCOL))
    tmp.replace(path)
    return path


def load_checkpoint(name: str, directory: Path | None = None) -> Simulation:
    path = _checkpoint_path(name, directory)
    if not path.is_file():
        raise NotFoundError(f"unknown checkpoint '{name}'")
    sim = pickle.loads(path.read_bytes())  # trusted, server-written files only
    if not isinstance(sim, Simulation):
        raise ValidationError("checkpoint does not contain a simulation")
    return sim


def list_checkpoints(directory: Path | None = None) -> list[str]:
    d = directory or CHECKPOINT_DIR
    return sorted(p.stem for p in d.glob("*.ckpt")) if d.is_dir() else []
