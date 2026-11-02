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
from .errors import ConflictError, NotFoundError, ValidationError

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"
CHECKPOINT_DIR = Path(__file__).resolve().parent.parent / "checkpoints"
_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
SCENARIO_FORMAT = "industrial-sim-scenario/v1"


def export_scenario(sim: Simulation) -> dict:
    assets, conveyors = [], []
    ind = sim.industrial.to_scenario()
    for a in sim.assets.values():
        entry = {"id": a.id, "kind": a.kind, "name": a.name, "config": a.to_config_dict(),
                 "position": dict(a.position)}
        if a.id in ind["bindings"]:
            entry["industrial"] = ind["bindings"][a.id]
        if a.id in ind["sync"]:
            entry["sync"] = ind["sync"][a.id]
        if isinstance(a, Conveyor):
            entry["source"] = {"asset_id": a.source[0], "port": a.source[1]}
            entry["target"] = {"asset_id": a.target[0], "port": a.target[1]}
            conveyors.append(entry)
        else:
            assets.append(entry)
    out = {
        "format": SCENARIO_FORMAT,
        "name": sim.name,
        **({"description": sim.description} if getattr(sim, "description", "") else {}),
        "seed": sim.seed,
        "product_types": [p.to_dict() for p in sim.product_types.values()],
        "recipes": [r.to_dict() for r in sim.recipes.values()],
        "production_orders": [o.to_dict() for o in sim.orders.values() if o.id != DEFAULT_ORDER_ID],
        "assets": assets + conveyors,
    }
    if ind["asset_types"]:
        out["asset_types"] = ind["asset_types"]
    ds = sim.datasets.to_scenario()
    if ds["sessions"] or ds["continuous"]["enabled"]:
        out["datasets"] = ds
    return out


def load_scenario(data: dict, *, seed: int | None = None, event_log_limit: int | None = None) -> Simulation:
    """Build a new simulation from a scenario document. Raises ValidationError atomically."""
    if not isinstance(data, dict):
        raise ValidationError("scenario must be a JSON object")
    fmt = data.get("format", SCENARIO_FORMAT)
    if fmt != SCENARIO_FORMAT:
        raise ValidationError(f"unsupported scenario format {fmt!r} (expected {SCENARIO_FORMAT!r})")
    for key in ("product_types", "production_orders", "recipes", "assets", "asset_types"):
        if not isinstance(data.get(key, []), list):
            raise ValidationError(f"'{key}' must be a list")
    s = data.get("seed") if seed is None else seed
    if s is not None and (isinstance(s, bool) or not isinstance(s, int)):
        raise ValidationError("seed must be an integer or null")
    sim = Simulation(s, event_log_limit=event_log_limit, name=str(data.get("name") or "untitled"))
    sim.description = str(data.get("description") or "")
    try:
        for pt in data.get("product_types", []):
            sim.add_product_type(pt)
        for o in data.get("production_orders", []):
            sim.add_order(o)
        for r in data.get("recipes", []):
            sim.add_recipe(r)
        sim.industrial.load_types(data.get("asset_types", []))
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
        for a in items:
            if a.get("industrial") or a.get("sync"):
                aid = a.get("id")
                if aid not in sim.assets:
                    raise ValidationError("assets with industrial or sync settings need an explicit id")
                sim.industrial.restore_binding(aid, a.get("industrial"), a.get("sync"))
        sim.datasets.load(data.get("datasets"))
    except ValidationError:
        raise
    except (AttributeError, TypeError, KeyError) as exc:
        raise ValidationError(f"malformed scenario: {exc}") from exc
    return sim


def list_builtin_scenarios(directory: Path | None = None) -> list[dict]:
    out = []
    for path in sorted((directory or SCENARIO_DIR).glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            out.append({"id": path.stem, "name": doc.get("name", path.stem), "description": doc.get("description", "")})
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _scenario_path(scenario_id: str, directory: Path | None = None) -> Path:
    if not _NAME.match(scenario_id or ""):
        raise ValidationError("scenario id must be 1-64 chars of letters, digits, '_' or '-'")
    return (directory or SCENARIO_DIR) / f"{scenario_id}.json"


def read_builtin_scenario(scenario_id: str, directory: Path | None = None) -> dict:
    path = _scenario_path(scenario_id, directory)
    if not path.is_file():
        raise NotFoundError(f"unknown scenario '{scenario_id}'")
    return json.loads(path.read_text(encoding="utf-8"))


def scenario_exists(scenario_id: str, directory: Path | None = None) -> bool:
    return _scenario_path(scenario_id, directory).is_file()


def write_scenario(doc: dict, scenario_id: str, directory: Path | None = None, *, overwrite: bool = True) -> Path:
    """Atomically write a scenario document to ``<directory>/<scenario_id>.json``."""
    path = _scenario_path(scenario_id, directory)
    if not overwrite and path.exists():
        raise ConflictError(f"scenario '{scenario_id}' already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def rename_scenario_file(old_id: str, new_id: str, directory: Path | None = None) -> Path:
    src, dst = _scenario_path(old_id, directory), _scenario_path(new_id, directory)
    if not src.is_file():
        raise NotFoundError(f"unknown scenario '{old_id}'")
    if src == dst:
        return dst
    if dst.exists():
        raise ConflictError(f"scenario '{new_id}' already exists")
    src.replace(dst)
    return dst


def new_scenario(current: Simulation, *, name: str, description: str = "", seed: int | None = None,
                 keep_catalog: bool = True, keep_asset_types: bool = True) -> Simulation:
    """A fresh, empty plant (no assets, conveyors or dataset sessions) at t=0.

    The catalogues of ``current`` – product types, recipes, production orders and (optionally) asset
    types – are carried over; the per-kind default asset types always exist in the new scenario."""
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("scenario name must be a non-empty string")
    src = export_scenario(current)
    doc = {"format": SCENARIO_FORMAT, "name": name.strip(), "description": str(description or ""),
           "seed": src["seed"] if seed is None else seed, "assets": []}
    if keep_catalog:
        doc.update(product_types=src["product_types"], recipes=src["recipes"],
                   production_orders=src["production_orders"])
    if keep_asset_types and src.get("asset_types"):
        doc["asset_types"] = src["asset_types"]
    sim = load_scenario(doc)
    sim.industrial.ensure_default_types()
    return sim


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
