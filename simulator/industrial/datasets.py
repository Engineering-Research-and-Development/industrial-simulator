"""Dataset collection: continuous collection and explicit collection sessions.

The collector observes the simulation (simulated and externally ingested data alike) and builds
four related datasets that can be joined through ``asset_id``, ``cycle_id``, ``product_id`` and
``production_order_ids``:

* ``sensor``  – one row per sensor/composite observation (raw), or per periodic sample (sampled).
* ``cycle``   – one row per finished production cycle with configurable measurement aggregations.
* ``product`` – one row per product produced by collected cycles, joined with cycle/machine data.
* ``machine`` – machine-level operational records (state intervals, working-condition transitions).

Raw observations are captured live (they are otherwise evicted from bounded histories). Cycle and
product rows are materialised at export time from the authoritative engine records, so later
product updates (e.g. external ``product_updated`` events or sink arrivals) are reflected and
retried/duplicated events can never produce duplicate rows.

Exporters are registered in :data:`EXPORTERS`; CSV is the reference format, JSONL is provided as
a second example so additional formats can be added without touching collection logic.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import math
import zipfile
from typing import Any, Callable

from ..errors import ConflictError, NotFoundError, ValidationError

DATASETS = ("sensor", "cycle", "product", "machine")
AGGREGATIONS = ("avg", "min", "max", "initial", "final", "count")
SAMPLING_MODES = ("event", "periodic", "both")
START_TYPES = ("manual", "at_time")
END_TYPES = ("manual", "at_time", "duration", "cycle_count", "product_count")
MISSING = ("empty", "null", "drop_row", "fill")
STATUSES = ("created", "scheduled", "running", "paused", "stopped", "completed")
MAX_SENSOR_ROWS = 200_000
MAX_MACHINE_ROWS = 50_000
MAX_SESSIONS = 50
DS_PRIORITY = 36

SENSOR_COLUMNS = ["timestamp", "asset_id", "measurement_id", "kind", "row_type", "cycle_id", "product_id",
                  "value", "raw_value", "unit", "source", "quality", "source_timestamp", "received_at",
                  "message_id", "observation_seq"]
CYCLE_COLUMNS = ["cycle_id", "asset_id", "kind", "recipe_id", "status", "source", "external_cycle_id",
                 "start_time", "end_time", "duration_s", "lot_number", "production_order_ids", "input_quantity",
                 "output_quantity", "defect_count", "end_reason", "config_version", "asset_type_id",
                 "asset_type_version", "working_conditions", "classification"]
PRODUCT_COLUMNS = ["product_id", "external_id", "product_type_id", "lot_number", "production_order_ids",
                   "cycle_id", "asset_id", "recipe_id", "production_timestamp", "status", "is_defective",
                   "quality_score", "parent_ids", "working_conditions"]
MACHINE_COLUMNS = ["timestamp", "asset_id", "record_type", "state", "reason", "start_time", "end_time",
                   "duration_s", "condition_id", "active", "severity"]
BASE_COLUMNS = {"sensor": SENSOR_COLUMNS, "cycle": CYCLE_COLUMNS, "product": PRODUCT_COLUMNS,
                "machine": MACHINE_COLUMNS}


def default_config() -> dict:
    return {
        "assets": [], "measurements": [], "include_composites": True,
        "product_types": [], "attributes": [], "production_order_ids": [],
        "datasets": list(DATASETS),
        "sampling": {"mode": "event", "interval_s": 1.0},
        "aggregations": ["avg", "min", "max"],
        "cycle_parameters": ["speed_pct", "processing_time_s", "quality_score", "interval_s", "batch_quantity"],
        "time_window": {"start_s": None, "end_s": None},
        "cycle_filter": {"recipe_ids": [], "sources": [], "statuses": ["completed"]},
        "start": {"type": "manual", "at_s": None},
        "end": {"type": "manual", "value": None},
        "columns": {d: {"select": [], "rename": {}} for d in DATASETS},
        "missing": {"policy": "empty", "fill_value": ""},
        "format": "csv",
    }


def _str_list(raw: Any, field: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise ValidationError(f"{field} must be a list of non-empty strings")
    return list(dict.fromkeys(x.strip() for x in raw))


def _num(raw: Any, field: str, *, minimum: float | None = None, allow_none: bool = True,
         exclusive: bool = False) -> float | None:
    if raw is None and allow_none:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ValidationError(f"{field} must be a finite number")
    if minimum is not None and (raw <= minimum if exclusive else raw < minimum):
        raise ValidationError(f"{field} must be {'>' if exclusive else '>='} {minimum}")
    return float(raw)


def normalize_config(raw: Any, current: dict | None = None) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValidationError("dataset configuration must be an object")
    base = copy.deepcopy(current or default_config())
    unknown = set(raw) - set(base)
    if unknown:
        raise ValidationError(f"unknown dataset configuration field(s) {sorted(unknown)}")
    cfg = base
    for key in ("assets", "measurements", "product_types", "attributes", "production_order_ids",
                "cycle_parameters"):
        if key in raw:
            cfg[key] = _str_list(raw[key], key)
    if "include_composites" in raw:
        if not isinstance(raw["include_composites"], bool):
            raise ValidationError("include_composites must be a boolean")
        cfg["include_composites"] = raw["include_composites"]
    if "datasets" in raw:
        ds = _str_list(raw["datasets"], "datasets")
        bad = [d for d in ds if d not in DATASETS]
        if bad or not ds:
            raise ValidationError(f"datasets must be a non-empty subset of {list(DATASETS)}")
        cfg["datasets"] = ds
    if "aggregations" in raw:
        ag = _str_list(raw["aggregations"], "aggregations")
        bad = [a for a in ag if a not in AGGREGATIONS]
        if bad:
            raise ValidationError(f"unknown aggregation(s) {bad}; expected {list(AGGREGATIONS)}")
        cfg["aggregations"] = ag
    for key, allowed in (("sampling", {"mode", "interval_s"}), ("time_window", {"start_s", "end_s"}),
                         ("cycle_filter", {"recipe_ids", "sources", "statuses"}), ("start", {"type", "at_s"}),
                         ("end", {"type", "value"}), ("missing", {"policy", "fill_value"})):
        if key in raw:
            if not isinstance(raw[key], dict):
                raise ValidationError(f"{key} must be an object")
            extra = set(raw[key]) - allowed
            if extra:
                raise ValidationError(f"unknown {key} field(s) {sorted(extra)}")
            cfg[key] = {**cfg[key], **copy.deepcopy(raw[key])}
    s = cfg["sampling"]
    if s["mode"] not in SAMPLING_MODES:
        raise ValidationError(f"sampling.mode must be one of {list(SAMPLING_MODES)}")
    s["interval_s"] = _num(s["interval_s"], "sampling.interval_s", minimum=0.0, allow_none=False, exclusive=True)
    tw = cfg["time_window"]
    tw["start_s"] = _num(tw.get("start_s"), "time_window.start_s", minimum=0.0)
    tw["end_s"] = _num(tw.get("end_s"), "time_window.end_s", minimum=0.0)
    if tw["start_s"] is not None and tw["end_s"] is not None and tw["end_s"] < tw["start_s"]:
        raise ValidationError("time_window.end_s must be >= start_s")
    cf = cfg["cycle_filter"]
    cf["recipe_ids"] = _str_list(cf.get("recipe_ids"), "cycle_filter.recipe_ids")
    cf["sources"] = _str_list(cf.get("sources"), "cycle_filter.sources")
    cf["statuses"] = _str_list(cf.get("statuses"), "cycle_filter.statuses")
    if any(x not in ("simulation", "external") for x in cf["sources"]):
        raise ValidationError("cycle_filter.sources entries must be 'simulation' or 'external'")
    if any(x not in ("completed", "cancelled") for x in cf["statuses"]):
        raise ValidationError("cycle_filter.statuses entries must be 'completed' or 'cancelled'")
    st = cfg["start"]
    if st["type"] not in START_TYPES:
        raise ValidationError(f"start.type must be one of {list(START_TYPES)}")
    st["at_s"] = _num(st.get("at_s"), "start.at_s", minimum=0.0)
    if st["type"] == "at_time" and st["at_s"] is None:
        raise ValidationError("start.at_s is required when start.type is 'at_time'")
    en = cfg["end"]
    if en["type"] not in END_TYPES:
        raise ValidationError(f"end.type must be one of {list(END_TYPES)}")
    if en["type"] != "manual":
        en["value"] = _num(en.get("value"), "end.value", minimum=0.0, allow_none=False, exclusive=True)
        if en["type"] in ("cycle_count", "product_count"):
            if en["value"] != int(en["value"]):
                raise ValidationError("end.value must be an integer for count-based end conditions")
            en["value"] = int(en["value"])
    else:
        en["value"] = None
    if "columns" in raw:
        if not isinstance(raw["columns"], dict):
            raise ValidationError("columns must be an object keyed by dataset")
        for d, spec in raw["columns"].items():
            if d not in DATASETS:
                raise ValidationError(f"columns: unknown dataset '{d}'")
            if not isinstance(spec, dict) or set(spec) - {"select", "rename"}:
                raise ValidationError(f"columns.{d} must be an object with 'select' and/or 'rename'")
            sel = _str_list(spec.get("select"), f"columns.{d}.select")
            ren = spec.get("rename") or {}
            if not isinstance(ren, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                                    for k, v in ren.items()):
                raise ValidationError(f"columns.{d}.rename must map column names to non-empty strings")
            if len(set(ren.values())) != len(ren):
                raise ValidationError(f"columns.{d}.rename produces duplicate column names")
            cfg["columns"][d] = {"select": sel, "rename": dict(ren)}
    m = cfg["missing"]
    if m["policy"] not in MISSING:
        raise ValidationError(f"missing.policy must be one of {list(MISSING)}")
    if not isinstance(m.get("fill_value"), (str, int, float)) or isinstance(m.get("fill_value"), bool):
        raise ValidationError("missing.fill_value must be a string or number")
    if "format" in raw:
        cfg["format"] = raw["format"]
    if cfg["format"] not in EXPORTERS:
        raise ValidationError(f"format must be one of {sorted(EXPORTERS)}")
    return cfg


class Session:
    def __init__(self, sid: str, name: str, description: str, config: dict, mode: str = "session",
                 experiment: str = "") -> None:
        self.id = sid
        self.name = name
        self.description = description
        self.mode = mode
        self.experiment = experiment
        self.config = config
        self.status = "created"
        self.created_at: float | None = None
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.segments: list[list[float | None]] = []
        self.sensor_rows: list[dict] = []
        self._sensor_keys: set = set()
        self.machine_rows: list[dict] = []
        self.cycle_ids: dict[str, None] = {}
        self.product_count = 0
        self.truncated: dict[str, bool] = {"sensor": False, "machine": False}
        self.duplicates_skipped = 0
        self.sample_event: int | None = None
        self.start_event: int | None = None
        self.end_event: int | None = None
        self.end_reason: str | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description, "mode": self.mode,
                "experiment": self.experiment, "status": self.status, "created_at": self.created_at,
                "started_at": self.started_at, "stopped_at": self.stopped_at, "end_reason": self.end_reason,
                "segments": copy.deepcopy(self.segments),
                "counts": {"sensor": len(self.sensor_rows), "cycle": len(self.cycle_ids),
                           "product": self.product_count, "machine": len(self.machine_rows)},
                "duplicates_skipped": self.duplicates_skipped, "truncated": dict(self.truncated),
                "config": copy.deepcopy(self.config)}


class DatasetCollector:
    def __init__(self, sim) -> None:
        self.sim = sim
        self.sessions: dict[str, Session] = {}
        self._seq = 0
        self.continuous = Session("continuous", "Continuous collection",
                                  "Always-on collection while enabled", default_config(), mode="continuous")

    # ------------------------------------------------------------------ session management
    def all_sessions(self) -> list[Session]:
        return [self.continuous, *self.sessions.values()]

    def get(self, sid: str) -> Session:
        if sid == "continuous":
            return self.continuous
        s = self.sessions.get(sid)
        if s is None:
            raise NotFoundError(f"unknown dataset session '{sid}'")
        return s

    def create_session(self, data: dict) -> Session:
        if not isinstance(data, dict):
            raise ValidationError("session must be an object")
        extra = set(data) - {"id", "name", "description", "config", "experiment", "autostart"}
        if extra:
            raise ValidationError(f"unknown session field(s) {sorted(extra)}")
        if len(self.sessions) >= MAX_SESSIONS:
            raise ConflictError(f"at most {MAX_SESSIONS} dataset sessions are supported; delete old sessions")
        name = data.get("name") or "Collection session"
        if not isinstance(name, str) or len(name) > 200:
            raise ValidationError("name must be a string of at most 200 characters")
        desc = data.get("description") or ""
        exp = data.get("experiment") or ""
        if not isinstance(desc, str) or not isinstance(exp, str):
            raise ValidationError("description and experiment must be strings")
        cfg = normalize_config(data.get("config"))
        self._check_refs(cfg)
        sid = data.get("id")
        if sid is None:
            self._seq += 1
            sid = f"DS-{self._seq:04d}"
            while sid in self.sessions:
                self._seq += 1
                sid = f"DS-{self._seq:04d}"
        elif not isinstance(sid, str) or not sid.strip() or sid == "continuous" or sid in self.sessions:
            raise ValidationError(f"session id '{sid}' is invalid or already used")
        s = Session(sid, name, desc, cfg, experiment=exp)
        s.created_at = self.sim.now
        self.sessions[sid] = s
        if data.get("autostart"):
            self.start(sid)
        return s

    def update_session(self, sid: str, data: dict) -> Session:
        s = self.get(sid)
        if not isinstance(data, dict) or set(data) - {"name", "description", "config", "experiment"}:
            raise ValidationError("only name, description, experiment and config can be updated")
        if "config" in data:
            if s.status in ("running", "paused") and s.mode != "continuous":
                raise ConflictError("stop the session before changing its configuration")
            cfg = normalize_config(data["config"], s.config)
            self._check_refs(cfg)
            s.config = cfg
        for key in ("name", "description", "experiment"):
            if key in data:
                if not isinstance(data[key], str):
                    raise ValidationError(f"{key} must be a string")
                setattr(s, key, data[key])
        if s.running:
            self._schedule_sampling(s)
        return s

    def delete_session(self, sid: str) -> None:
        s = self.get(sid)
        if s.mode == "continuous":
            raise ConflictError("the continuous collector cannot be deleted; disable or clear it")
        self._cancel(s)
        del self.sessions[sid]

    def clear(self, sid: str) -> Session:
        s = self.get(sid)
        s.sensor_rows.clear()
        s._sensor_keys.clear()
        s.machine_rows.clear()
        s.cycle_ids.clear()
        s.product_count = 0
        s.duplicates_skipped = 0
        s.truncated = {"sensor": False, "machine": False}
        return s

    def _check_refs(self, cfg: dict) -> None:
        missing = [a for a in cfg["assets"] if a not in self.sim.assets]
        if missing:
            raise ValidationError(f"unknown asset(s) {missing}")
        bad = [t for t in cfg["product_types"] if t not in self.sim.product_types]
        if bad:
            raise ValidationError(f"unknown product type(s) {bad}")

    # ------------------------------------------------------------------ lifecycle
    def start(self, sid: str) -> Session:
        s = self.get(sid)
        if s.status in ("running", "scheduled"):
            raise ConflictError(f"session '{sid}' is already {s.status}")
        if s.status in ("stopped", "completed") and s.mode != "continuous":
            raise ConflictError(f"session '{sid}' is {s.status}; create a new session")
        st = s.config["start"]
        if st["type"] == "at_time" and st["at_s"] > self.sim.now and s.status == "created":
            s.status = "scheduled"
            if self.sim.started:
                s.start_event = self.sim.scheduler.schedule(st["at_s"], DS_PRIORITY, "ds:start", "", s.id)
            return s
        self._begin(s)
        return s

    def _begin(self, s: Session) -> None:
        s.status = "running"
        s.start_event = None
        if s.started_at is None:
            s.started_at = self.sim.now
        s.stopped_at = None
        s.end_reason = None
        s.segments.append([self.sim.now, None])
        self._schedule_sampling(s)
        self._schedule_end(s)
        if s.config["sampling"]["mode"] in ("periodic", "both"):
            self._sample(s)
        self._record_states(s, "start")

    def pause(self, sid: str) -> Session:
        s = self.get(sid)
        if s.status != "running":
            raise ConflictError(f"session '{sid}' is not running")
        s.status = "paused"
        self._close_segment(s)
        self._cancel(s)
        return s

    def resume(self, sid: str) -> Session:
        s = self.get(sid)
        if s.status != "paused":
            raise ConflictError(f"session '{sid}' is not paused")
        self._begin(s)
        return s

    def stop(self, sid: str, reason: str = "manual") -> Session:
        s = self.get(sid)
        if s.status not in ("running", "paused", "scheduled"):
            raise ConflictError(f"session '{sid}' is not active")
        self._finish(s, "stopped", reason)
        return s

    def _finish(self, s: Session, status: str, reason: str) -> None:
        if s.running:
            self._record_states(s, "stop")
        self._close_segment(s)
        self._cancel(s)
        s.status = "stopped" if s.mode == "continuous" else status
        s.stopped_at = self.sim.now
        s.end_reason = reason

    def _close_segment(self, s: Session) -> None:
        if s.segments and s.segments[-1][1] is None:
            s.segments[-1][1] = self.sim.now

    def _cancel(self, s: Session) -> None:
        for attr in ("sample_event", "start_event", "end_event"):
            seq = getattr(s, attr)
            if seq is not None:
                self.sim.scheduler.cancel(seq)
                setattr(s, attr, None)

    def _schedule_sampling(self, s: Session) -> None:
        if s.sample_event is not None:
            self.sim.scheduler.cancel(s.sample_event)
            s.sample_event = None
        if self.sim.started and s.running and s.config["sampling"]["mode"] in ("periodic", "both"):
            s.sample_event = self.sim.scheduler.schedule(self.sim.now + s.config["sampling"]["interval_s"],
                                                         DS_PRIORITY, "ds:sample", "", s.id)

    def _schedule_end(self, s: Session) -> None:
        if s.end_event is not None:
            self.sim.scheduler.cancel(s.end_event)
            s.end_event = None
        en = s.config["end"]
        if not self.sim.started or en["type"] not in ("at_time", "duration"):
            return
        at = en["value"] if en["type"] == "at_time" else s.started_at + en["value"]
        if at <= self.sim.now:
            self._finish(s, "completed", en["type"])
            return
        s.end_event = self.sim.scheduler.schedule(at, DS_PRIORITY, "ds:end", "", s.id)

    def activate(self) -> None:
        """Called when the simulation starts: arm timers of sessions created before the start."""
        for s in self.all_sessions():
            if s.status == "scheduled" and s.start_event is None:
                at = s.config["start"]["at_s"]
                if at <= self.sim.now:
                    self._begin(s)
                else:
                    s.start_event = self.sim.scheduler.schedule(at, DS_PRIORITY, "ds:start", "", s.id)
            elif s.running:
                self._schedule_sampling(s)
                self._schedule_end(s)

    def handle_event(self, ev) -> None:
        s = self.sessions.get(ev.payload) or (self.continuous if ev.payload == "continuous" else None)
        if s is None:
            return
        if ev.kind == "ds:start":
            s.start_event = None
            if s.status == "scheduled":
                self._begin(s)
        elif ev.kind == "ds:end":
            s.end_event = None
            if s.status in ("running", "paused"):
                self._finish(s, "completed", s.config["end"]["type"])
        elif ev.kind == "ds:sample":
            s.sample_event = None
            if s.running:
                self._sample(s)
                self._schedule_sampling(s)

    # ------------------------------------------------------------------ live collection
    def _asset_ok(self, s: Session, aid: str) -> bool:
        return not s.config["assets"] or aid in s.config["assets"]

    def _measurement_ok(self, s: Session, aid: str, mid: str, kind: str) -> bool:
        if kind == "composite" and not s.config["include_composites"]:
            return False
        sel = s.config["measurements"]
        return not sel or mid in sel or f"{aid}.{mid}" in sel

    def _time_ok(self, s: Session, t: float | None) -> bool:
        tw = s.config["time_window"]
        if t is None:
            return True
        return (tw["start_s"] is None or t >= tw["start_s"]) and (tw["end_s"] is None or t <= tw["end_s"])

    def on_observation(self, obs) -> None:
        for s in self.all_sessions():
            if not s.running or "sensor" not in s.config["datasets"]:
                continue
            if s.config["sampling"]["mode"] == "periodic":
                continue
            if not self._asset_ok(s, obs.asset_id) or not self._measurement_ok(s, obs.asset_id, obs.measurement_id,
                                                                                obs.kind):
                continue
            if not self._time_ok(s, obs.sim_time):
                continue
            key = (obs.asset_id, obs.measurement_id, obs.message_id) if obs.message_id else ("seq", obs.seq)
            if key in s._sensor_keys:
                s.duplicates_skipped += 1
                continue
            if len(s.sensor_rows) >= MAX_SENSOR_ROWS:
                s.truncated["sensor"] = True
                continue
            s._sensor_keys.add(key)
            s.sensor_rows.append({
                "timestamp": obs.sim_time, "asset_id": obs.asset_id, "measurement_id": obs.measurement_id,
                "kind": obs.kind, "row_type": "raw", "cycle_id": obs.cycle_id, "product_id": obs.product_id,
                "value": obs.value, "raw_value": obs.raw_value, "unit": obs.unit, "source": obs.source,
                "quality": obs.quality, "source_timestamp": obs.source_timestamp, "received_at": obs.received_at,
                "message_id": obs.message_id, "observation_seq": obs.seq})

    def _sample(self, s: Session) -> None:
        if "sensor" not in s.config["datasets"] or not self._time_ok(s, self.sim.now):
            return
        hub = self.sim.industrial
        for (aid, mid), obs in sorted(hub.store.latest.items()):
            if aid not in self.sim.assets or not self._asset_ok(s, aid):
                continue
            if not self._measurement_ok(s, aid, mid, obs.kind):
                continue
            if len(s.sensor_rows) >= MAX_SENSOR_ROWS:
                s.truncated["sensor"] = True
                return
            asset = self.sim.assets[aid]
            cid = getattr(asset, "active_cycle_id", None)
            s.sensor_rows.append({
                "timestamp": self.sim.now, "asset_id": aid, "measurement_id": mid, "kind": obs.kind,
                "row_type": "sampled", "cycle_id": cid, "product_id": None, "value": obs.value,
                "raw_value": None, "unit": obs.unit, "source": obs.source, "quality": obs.quality,
                "source_timestamp": obs.source_timestamp, "received_at": obs.sim_time,
                "message_id": None, "observation_seq": obs.seq})

    def _machine_row(self, s: Session, row: dict) -> None:
        if len(s.machine_rows) >= MAX_MACHINE_ROWS:
            s.truncated["machine"] = True
            return
        s.machine_rows.append({c: row.get(c) for c in MACHINE_COLUMNS})

    def _record_states(self, s: Session, marker: str) -> None:
        if "machine" not in s.config["datasets"]:
            return
        for a in self.sim.assets.values():
            if self._asset_ok(s, a.id):
                self._machine_row(s, {"timestamp": self.sim.now, "asset_id": a.id, "record_type": f"state_at_{marker}",
                                      "state": a.state, "reason": a.state_reason})

    def on_condition(self, aid: str, cid: str, on: bool) -> None:
        cond = None
        rt = self.sim.industrial.runtime.get(aid)
        if rt is not None:
            cond = next((c for c in rt.profile["conditions"] if c["id"] == cid), None)
        for s in self.all_sessions():
            if s.running and "machine" in s.config["datasets"] and self._asset_ok(s, aid) \
                    and self._time_ok(s, self.sim.now):
                self._machine_row(s, {"timestamp": self.sim.now, "asset_id": aid, "record_type": "condition",
                                      "condition_id": cid, "active": on,
                                      "severity": cond["severity"] if cond else None})

    def on_cycle_finished(self, cycle) -> None:
        for s in self.all_sessions():
            if not s.running or not self._asset_ok(s, cycle.asset_id):
                continue
            if cycle.id in s.cycle_ids:
                s.duplicates_skipped += 1
                continue
            s.cycle_ids[cycle.id] = None
            s.product_count += len(cycle.output_ids)
            en = s.config["end"]
            if en["type"] == "cycle_count" and len(s.cycle_ids) >= en["value"]:
                self._finish(s, "completed", "cycle_count")
            elif en["type"] == "product_count" and s.product_count >= en["value"]:
                self._finish(s, "completed", "product_count")

    # ------------------------------------------------------------------ dataset building
    def _cycle_ok(self, s: Session, c) -> bool:
        cf = s.config["cycle_filter"]
        if cf["recipe_ids"] and c.recipe_id not in cf["recipe_ids"]:
            return False
        if cf["sources"] and c.source not in cf["sources"]:
            return False
        if cf["statuses"] and c.status not in cf["statuses"]:
            return False
        orders = s.config["production_order_ids"]
        if orders and not set(orders) & set(c.production_order_ids):
            return False
        return self._time_ok(s, c.ended_at if c.ended_at is not None else c.started_at)

    def _cycles(self, s: Session) -> list:
        return [self.sim.cycles[cid] for cid in s.cycle_ids if cid in self.sim.cycles
                and self._cycle_ok(s, self.sim.cycles[cid])]

    @staticmethod
    def _param(c, name: str) -> Any:
        snap = c.config_snapshot or {}
        ind = snap.get("industrial") or {}
        for src in (ind.get("features") or {}, snap, ind.get("parameters") or {}):
            if name in src and not isinstance(src[name], (dict, list)):
                return src[name]
        return None

    def _measurement_units(self, rows_cycles: list) -> dict[str, str]:
        units: dict[str, str] = {}
        for c in rows_cycles:
            for mid, agg in (c.measurements or {}).items():
                units.setdefault(mid, agg.get("unit") or "")
        return units

    def build(self, sid: str, dataset: str) -> tuple[list[str], list[dict], dict[str, str]]:
        """Return (columns, rows, column units) for one dataset of a session, before column mapping."""
        s = self.get(sid)
        if dataset not in DATASETS:
            raise ValidationError(f"dataset must be one of {list(DATASETS)}")
        if dataset == "sensor":
            units = {"value": "(see unit column)", "timestamp": "s", "received_at": "s"}
            return list(SENSOR_COLUMNS), [dict(r) for r in s.sensor_rows], units
        if dataset == "machine":
            return list(MACHINE_COLUMNS), self._machine_rows(s), {"timestamp": "s", "start_time": "s",
                                                                  "end_time": "s", "duration_s": "s"}
        cycles = self._cycles(s)
        if dataset == "cycle":
            return self._cycle_rows(s, cycles)
        return self._product_rows(s, cycles)

    def _machine_rows(self, s: Session) -> list[dict]:
        rows = [dict(r) for r in s.machine_rows]
        for aid, intervals in self.sim.operational_history.items():
            if not self._asset_ok(s, aid):
                continue
            for iv in intervals:
                end = iv.end if iv.end is not None else self.sim.now
                if not any(self._overlaps(iv.start, end, a, b if b is not None else self.sim.now)
                           for a, b in s.segments):
                    continue
                if not (self._time_ok(s, iv.start) or self._time_ok(s, end)):
                    continue
                rows.append({c: None for c in MACHINE_COLUMNS} | {
                    "timestamp": iv.start, "asset_id": aid, "record_type": "state_interval", "state": iv.state,
                    "reason": iv.reason, "start_time": iv.start, "end_time": iv.end,
                    "duration_s": end - iv.start})
        rows.sort(key=lambda r: (r["timestamp"] if r["timestamp"] is not None else 0.0, r["asset_id"]))
        return rows

    @staticmethod
    def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
        return a0 <= b1 and b0 <= a1

    def _cycle_rows(self, s: Session, cycles: list) -> tuple[list[str], list[dict], dict[str, str]]:
        cfg = s.config
        aggs = cfg["aggregations"]
        mids: dict[str, str] = {}
        for c in cycles:
            for mid, agg in (c.measurements or {}).items():
                if self._measurement_ok(s, c.asset_id, mid, "sensor"):
                    mids.setdefault(mid, agg.get("unit") or "")
        params = cfg["cycle_parameters"]
        mcols = [f"{mid}_{a}" for mid in sorted(mids) for a in aggs]
        units = {"start_time": "s", "end_time": "s", "duration_s": "s"}
        for mid, u in mids.items():
            for a in aggs:
                units[f"{mid}_{a}"] = "" if a == "count" else u
        units.update({"speed_pct": "%", "processing_time_s": "s", "interval_s": "s"})
        rows = []
        for c in cycles:
            ind = (c.config_snapshot or {}).get("industrial") or {}
            at = ind.get("asset_type") or {}
            row = {"cycle_id": c.id, "asset_id": c.asset_id, "kind": c.kind, "recipe_id": c.recipe_id,
                   "status": c.status, "source": c.source, "external_cycle_id": c.external_cycle_id,
                   "start_time": c.started_at, "end_time": c.ended_at,
                   "duration_s": (c.ended_at - c.started_at) if c.ended_at is not None else None,
                   "lot_number": c.lot_number, "production_order_ids": c.production_order_ids,
                   "input_quantity": len(c.input_ids), "output_quantity": len(c.output_ids),
                   "defect_count": c.defect_count, "end_reason": c.end_reason, "config_version": c.config_version,
                   "asset_type_id": at.get("id"), "asset_type_version": at.get("version"),
                   "working_conditions": ind.get("working_conditions"), "classification": ind.get("classification")}
            for p in params:
                row[p] = self._param(c, p)
            for mid in sorted(mids):
                agg = (c.measurements or {}).get(mid) or {}
                for a in aggs:
                    row[f"{mid}_{a}"] = agg.get(a)
            rows.append(row)
        return CYCLE_COLUMNS + params + mcols, rows, units

    def _product_rows(self, s: Session, cycles: list) -> tuple[list[str], list[dict], dict[str, str]]:
        cfg = s.config
        types = cfg["product_types"]
        ccols, crows, cunits = self._cycle_rows(s, cycles)
        by_cycle = {r["cycle_id"]: r for r in crows}
        joined = [c for c in ccols if c not in PRODUCT_COLUMNS and c not in
                  ("kind", "status", "source", "external_cycle_id", "start_time", "end_time", "input_quantity",
                   "output_quantity", "defect_count", "end_reason", "config_version", "classification")]
        attr_units: dict[str, str] = {}
        attr_names: dict[str, None] = {}
        products = []
        for c in cycles:
            for pid in c.output_ids:
                p = self.sim.products.get(pid)
                if p is None or (types and p.product_type_id not in types):
                    continue
                products.append(p)
                pt = self.sim.product_types.get(p.product_type_id)
                for d in (pt.attributes if pt else []):
                    attr_units.setdefault(d["name"], d.get("unit") or "")
                for k in p.attributes:
                    attr_names.setdefault(k, None)
        sel_attrs = cfg["attributes"]
        names = [n for n in attr_names if not sel_attrs or n in sel_attrs]
        attr_cols = [f"attr_{n}" for n in names]
        rows = []
        for p in products:
            crow = by_cycle.get(p.origin_cycle_id, {})
            row = {"product_id": p.id, "external_id": p.external_id, "product_type_id": p.product_type_id,
                   "lot_number": p.lot_number, "production_order_ids": p.production_order_ids,
                   "cycle_id": p.origin_cycle_id, "asset_id": crow.get("asset_id"), "recipe_id": crow.get("recipe_id"),
                   "production_timestamp": p.production_timestamp, "status": p.status.value,
                   "is_defective": p.is_defective, "quality_score": p.quality_score, "parent_ids": p.parent_ids,
                   "working_conditions": crow.get("working_conditions")}
            for n in names:
                row[f"attr_{n}"] = p.attributes.get(n)
                prov = p.attribute_provenance.get(n)
                row[f"attr_{n}_source"] = prov.get("source_type") or prov.get("kind") if prov else None
            for j in joined:
                if j not in row:
                    row[f"cycle_{j}"] = crow.get(j)
            rows.append(row)
        prov_cols = [f"attr_{n}_source" for n in names]
        cyc_cols = [f"cycle_{j}" for j in joined if j not in PRODUCT_COLUMNS]
        units = {"production_timestamp": "s"}
        units.update({f"attr_{n}": attr_units.get(n, "") for n in names})
        units.update({f"cycle_{k}": v for k, v in cunits.items()})
        cols = PRODUCT_COLUMNS + [x for pair in zip(attr_cols, prov_cols) for x in pair] + cyc_cols
        return cols, rows, units

    # ------------------------------------------------------------------ shaping & export
    def table(self, sid: str, dataset: str) -> tuple[list[str], list[dict], dict[str, str]]:
        """Columns/rows after column selection, renaming and missing-value handling."""
        s = self.get(sid)
        cols, rows, units = self.build(sid, dataset)
        spec = s.config["columns"][dataset]
        if spec["select"]:
            unknown = [c for c in spec["select"] if c not in cols]
            if unknown:
                raise ValidationError(f"columns.{dataset}.select references unknown column(s) {unknown}")
            cols = list(spec["select"])
        miss = s.config["missing"]
        out_rows = []
        for r in rows:
            vals = {c: _cell(r.get(c)) for c in cols}
            if miss["policy"] == "drop_row" and any(v is None for v in vals.values()):
                continue
            if miss["policy"] == "fill":
                vals = {c: miss["fill_value"] if v is None else v for c, v in vals.items()}
            out_rows.append(vals)
        ren = spec["rename"]
        final_cols = [ren.get(c, c) for c in cols]
        if len(set(final_cols)) != len(final_cols):
            raise ValidationError(f"columns.{dataset}.rename produces duplicate column names")
        out_rows = [{ren.get(c, c): v for c, v in r.items()} for r in out_rows]
        out_units = {ren.get(c, c): units[c] for c in cols if c in units}
        return final_cols, out_rows, out_units

    def preview(self, sid: str, dataset: str, limit: int = 50) -> dict:
        cols, rows, units = self.table(sid, dataset)
        return {"session_id": sid, "dataset": dataset, "columns": cols, "units": units, "total_rows": len(rows),
                "rows": rows[-max(0, min(int(limit), 1000)):] if limit else []}

    def export(self, sid: str, dataset: str, fmt: str | None = None) -> tuple[bytes, str, str]:
        s = self.get(sid)
        fmt = fmt or s.config["format"]
        exp = EXPORTERS.get(fmt)
        if exp is None:
            raise ValidationError(f"format must be one of {sorted(EXPORTERS)}")
        cols, rows, _ = self.table(sid, dataset)
        data = exp["write"](cols, rows, s.config["missing"]["policy"])
        return data, f"{s.id}_{dataset}.{exp['ext']}", exp["mime"]

    def manifest(self, sid: str, datasets: list[str]) -> dict:
        s = self.get(sid)
        tables = {}
        for d in datasets:
            cols, rows, units = self.table(sid, d)
            tables[d] = {"columns": cols, "units": units, "rows": len(rows)}
        return {"session": {k: v for k, v in s.summary().items() if k != "config"}, "config": s.config,
                "generated_at_sim_time": self.sim.now, "seed": self.sim.seed, "datasets": tables,
                "relationships": [
                    {"from": "sensor.cycle_id", "to": "cycle.cycle_id"},
                    {"from": "sensor.asset_id", "to": "cycle.asset_id"},
                    {"from": "product.cycle_id", "to": "cycle.cycle_id"},
                    {"from": "product.production_order_ids", "to": "cycle.production_order_ids"},
                    {"from": "machine.asset_id", "to": "cycle.asset_id"}],
                "notes": ["timestamps are simulation seconds; source_timestamp holds external ISO timestamps",
                          "sensor.row_type distinguishes raw observations from periodic samples",
                          "cycle aggregate columns are named <measurement>_<aggregation>",
                          "product attr_<name>_source records attribute provenance"]}

    def bundle(self, sid: str, datasets: list[str] | None = None, fmt: str | None = None) -> tuple[bytes, str]:
        s = self.get(sid)
        datasets = datasets or [d for d in s.config["datasets"]]
        bad = [d for d in datasets if d not in DATASETS]
        if bad:
            raise ValidationError(f"unknown dataset(s) {bad}")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for d in datasets:
                data, name, _ = self.export(sid, d, fmt)
                z.writestr(name, data)
            z.writestr("manifest.json", json.dumps(self.manifest(sid, datasets), indent=2, default=str))
        return buf.getvalue(), f"{s.id}_datasets.zip"

    # ------------------------------------------------------------------ views / persistence
    def summary(self) -> dict:
        return {"continuous": self.continuous.summary(),
                "sessions": [s.summary() for s in self.sessions.values()],
                "datasets": list(DATASETS), "formats": sorted(EXPORTERS), "aggregations": list(AGGREGATIONS)}

    def to_scenario(self) -> dict:
        out = {"continuous": {"config": copy.deepcopy(self.continuous.config),
                              "enabled": self.continuous.status in ("running", "scheduled")},
               "sessions": []}
        for s in self.sessions.values():
            out["sessions"].append({"id": s.id, "name": s.name, "description": s.description,
                                    "experiment": s.experiment, "config": copy.deepcopy(s.config)})
        return out

    def load(self, data: dict | None) -> None:
        if not data:
            return
        if not isinstance(data, dict):
            raise ValidationError("datasets must be an object")
        cont = data.get("continuous") or {}
        if cont.get("config") is not None:
            self.continuous.config = normalize_config(cont["config"])
        for raw in data.get("sessions") or []:
            self.create_session({k: v for k, v in raw.items() if k in ("id", "name", "description", "config",
                                                                       "experiment")})
        if cont.get("enabled"):
            self.start("continuous")


def _cell(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (list, tuple)):
        return ";".join(str(x) for x in v) if v else None
    if isinstance(v, dict):
        return json.dumps(v, sort_keys=True, default=str) if v else None
    return v


def _csv_value(v: Any, policy: str) -> Any:
    if v is None:
        return "NULL" if policy == "null" else ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(round(v, 9))
    return v


def write_csv(cols: list[str], rows: list[dict], policy: str = "empty") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([_csv_value(r.get(c), policy) for c in cols])
    return buf.getvalue().encode("utf-8")


def write_jsonl(cols: list[str], rows: list[dict], policy: str = "empty") -> bytes:
    return "".join(json.dumps({c: r.get(c) for c in cols}, default=str) + "\n" for r in rows).encode("utf-8")


EXPORTERS: dict[str, dict[str, Any]] = {
    "csv": {"write": write_csv, "ext": "csv", "mime": "text/csv"},
    "jsonl": {"write": write_jsonl, "ext": "jsonl", "mime": "application/x-ndjson"},
}


def register_exporter(name: str, write: Callable[[list[str], list[dict], str], bytes], ext: str, mime: str) -> None:
    EXPORTERS[name] = {"write": write, "ext": ext, "mime": mime}


__all__ = ["DatasetCollector", "normalize_config", "default_config", "EXPORTERS", "register_exporter", "DATASETS"]
