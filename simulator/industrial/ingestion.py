"""Canonical external-event ingestion (MQTT, REST): telemetry, production lifecycle, products,
configuration and state.

Every transport converts its payload to the canonical envelope and calls :func:`ingest` while
holding the controller lock, so external events are applied atomically between simulation
events. Messages are validated before any mutation; rejected messages never change state.

Canonical envelope (``schema_version`` "1.0")::

    {"schema_version": "1.0", "event_id": "...", "asset_id": "<internal or external id>",
     "event_type": "telemetry", "timestamp": "2024-01-01T10:00:00Z" | 1704103200.0,
     "cycle_id": "<external cycle id>",                       # optional
     "measurements": {"temp": 81.2, "pressure": {"value": 2.1, "unit": "bar", "timestamp": ...}},
     "inputs": [{"product_id": "..."}], "outputs": [{"product_id": "...", "product_type_id": "...",
     "slot": 0, "is_defective": false, "attributes": {...}}],
     "product_id": "...", "attributes": {...},                 # product_updated
     "config": {...}, "parameters": {...},                     # configuration_changed
     "state": "running" | "stopped" | "failed"}                # state_changed
"""

from __future__ import annotations

import copy
import math
from collections import Counter, OrderedDict, deque
from datetime import datetime, timezone
from typing import Any

from ..errors import ConflictError, NotFoundError, SimulatorError, ValidationError
from .definitions import coerce_value
from .units import convert

SCHEMA_VERSION = "1.0"
EVENT_TYPES = ("telemetry", "cycle_started", "cycle_completed", "cycle_cancelled", "batch_produced",
               "product_updated", "configuration_changed", "state_changed")
CATEGORY = {"telemetry": "measurements", "cycle_started": "production", "cycle_completed": "production",
            "cycle_cancelled": "production", "batch_produced": "production", "product_updated": "production",
            "configuration_changed": "configuration", "state_changed": "state"}
STATES = {"running": "start", "idle": "start", "started": "start", "stopped": "stop", "failed": "fault",
          "fault": "fault"}
MAX_SEEN = 20_000
MAX_MESSAGE_BYTES = 256 * 1024


class IngestionState:
    def __init__(self) -> None:
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.cycle_map: dict[tuple[str, str], str] = {}
        self.pending_completions: dict[tuple[str, str], dict] = {}
        self.product_map: dict[str, str] = {}
        self.messages: deque[dict] = deque(maxlen=500)
        self.counts: Counter[str] = Counter()
        self.time_epoch: float | None = None
        self.skew: dict[str, dict] = {}
        self.last_source_ts: dict[tuple[str, str], float] = {}

    def remember(self, event_id: str) -> None:
        self.seen[event_id] = None
        while len(self.seen) > MAX_SEEN:
            self.seen.popitem(last=False)

    def status(self) -> dict:
        return {"counts": dict(self.counts), "time_epoch": self.time_epoch,
                "mapped_cycles": len(self.cycle_map), "pending_completions": len(self.pending_completions),
                "mapped_products": len(self.product_map), "clock_skew": copy.deepcopy(self.skew)}


def parse_timestamp(value: Any, field: str = "timestamp") -> float | None:
    """ISO-8601 string or epoch seconds (or milliseconds) -> epoch seconds."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be an ISO-8601 string or epoch seconds")
    if isinstance(value, (int, float)):
        v = float(value)
        if not math.isfinite(v) or v < 0:
            raise ValidationError(f"{field} must be a finite non-negative epoch")
        return v / 1000.0 if v > 1e11 else v
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            raise ValidationError(f"{field} '{value}' is not a valid ISO-8601 timestamp") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise ValidationError(f"{field} must be an ISO-8601 string or epoch seconds")


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _check_envelope(env: Any) -> dict:
    if not isinstance(env, dict):
        raise ValidationError("message must be a JSON object")
    sv = env.get("schema_version", SCHEMA_VERSION)
    if sv != SCHEMA_VERSION:
        raise ValidationError(f"unsupported schema_version '{sv}' (expected '{SCHEMA_VERSION}')")
    eid = env.get("event_id")
    if not isinstance(eid, str) or not eid.strip() or len(eid) > 200:
        raise ValidationError("event_id must be a non-empty string")
    aid = env.get("asset_id")
    if not isinstance(aid, str) or not aid.strip():
        raise ValidationError("asset_id must be a non-empty string")
    et = env.get("event_type")
    if et not in EVENT_TYPES:
        raise ValidationError(f"event_type must be one of {list(EVENT_TYPES)}")
    for key in ("measurements", "attributes", "config", "parameters"):
        if env.get(key) is not None and not isinstance(env[key], dict):
            raise ValidationError(f"'{key}' must be an object")
    for key in ("inputs", "outputs"):
        if env.get(key) is not None:
            if not isinstance(env[key], list) or not all(isinstance(x, (dict, str)) for x in env[key]):
                raise ValidationError(f"'{key}' must be a list of objects")
    cid = env.get("cycle_id")
    if cid is not None and (not isinstance(cid, str) or not cid.strip()):
        raise ValidationError("cycle_id must be a non-empty string")
    return env


def _product_ref(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    pid = item.get("product_id")
    return pid if isinstance(pid, str) and pid else None


def _outputs(env: dict) -> list[dict]:
    out = []
    for k, item in enumerate(env.get("outputs") or []):
        if isinstance(item, str):
            item = {"product_id": item}
        spec = {"product_id": _product_ref(item)}
        for key in ("product_type_id", "slot", "is_defective", "attributes"):
            if item.get(key) is not None:
                spec[key] = copy.deepcopy(item[key])
        if "is_defective" in spec and not isinstance(spec["is_defective"], bool):
            raise ValidationError(f"outputs[{k}].is_defective must be a boolean")
        if "attributes" in spec and not isinstance(spec["attributes"], dict):
            raise ValidationError(f"outputs[{k}].attributes must be an object")
        out.append(spec)
    return out


def ingest(sim, envelope: Any, *, transport: str = "rest", topic: str | None = None) -> dict:
    """Validate and apply one canonical envelope. Never raises for bad messages; returns a result."""
    hub = sim.industrial
    st: IngestionState = hub.ingestion
    rec: dict[str, Any] = {"received_at": sim.now, "transport": transport, "topic": topic,
                           "event_id": envelope.get("event_id") if isinstance(envelope, dict) else None,
                           "asset_id": envelope.get("asset_id") if isinstance(envelope, dict) else None,
                           "event_type": envelope.get("event_type") if isinstance(envelope, dict) else None,
                           "errors": [], "warnings": []}
    try:
        env = _check_envelope(envelope)
        if env["event_id"] in st.seen:
            rec["status"] = "duplicate"
        else:
            aid = hub.find_asset_by_external_id(env["asset_id"])
            if aid is None:
                raise NotFoundError(f"unknown asset '{env['asset_id']}' (no asset or external id mapping)")
            rec["asset_id"] = aid
            _apply(sim, aid, env, transport, rec)
            st.remember(env["event_id"])
            rec["status"] = "partial" if rec["errors"] else rec.pop("status_override", "accepted")
    except SimulatorError as exc:
        rec["status"] = "rejected"
        rec["errors"].append(str(exc))
        rec["error_type"] = type(exc).__name__
    st.counts[rec["status"]] += 1
    st.counts[f"transport:{transport}"] += 1
    st.messages.append(rec)
    return rec


def _apply(sim, aid: str, env: dict, transport: str, rec: dict) -> None:
    hub = sim.industrial
    et = env["event_type"]
    cat = CATEGORY[et]
    if et != "telemetry" and not hub.category_external(aid, cat):
        raise ConflictError(f"'{aid}' does not accept external {cat} events (sync category '{cat}' is simulation)")
    ts = parse_timestamp(env.get("timestamp"))
    rec["source_timestamp"] = _iso(ts)
    if ts is not None:
        _track_skew(sim, aid, ts)
    if et == "telemetry":
        n = _telemetry(sim, aid, env, ts, transport, rec)
        if not n and rec["errors"]:
            raise ValidationError("; ".join(rec["errors"]))
        return
    if not sim.started and et in ("cycle_started", "cycle_completed", "cycle_cancelled", "batch_produced"):
        raise ConflictError("production events require a started simulation")
    if et == "cycle_started":
        _cycle_started(sim, aid, env, ts, transport, rec)
    elif et in ("cycle_completed", "batch_produced"):
        _cycle_completed(sim, aid, env, ts, transport, rec)
    elif et == "cycle_cancelled":
        _cycle_cancelled(sim, aid, env, rec)
    elif et == "product_updated":
        _product_updated(sim, aid, env, rec)
    elif et == "configuration_changed":
        if env.get("config"):
            sim.update_config(aid, env["config"], origin="external")
        if env.get("parameters"):
            hub.set_parameters(aid, env["parameters"], origin="external")
        if not env.get("config") and not env.get("parameters"):
            raise ValidationError("configuration_changed requires 'config' and/or 'parameters'")
    elif et == "state_changed":
        cmd = STATES.get(env.get("state"))
        if cmd is None:
            raise ValidationError(f"state must be one of {sorted(STATES)}")
        sim.get_asset(aid).command(cmd)
        rec["applied_command"] = cmd
    if env.get("measurements") and et not in ("cycle_started", "cycle_completed", "batch_produced"):
        _telemetry(sim, aid, env, ts, transport, rec)


def _track_skew(sim, aid: str, ts: float) -> None:
    st = sim.industrial.ingestion
    if st.time_epoch is None:
        return
    skew = (ts - st.time_epoch) - sim.now
    s = st.skew.setdefault(aid, {"count": 0, "last_s": 0.0, "max_abs_s": 0.0, "mean_s": 0.0})
    s["count"] += 1
    s["last_s"] = skew
    s["max_abs_s"] = max(s["max_abs_s"], abs(skew))
    s["mean_s"] += (skew - s["mean_s"]) / s["count"]


def _telemetry(sim, aid: str, env: dict, ts: float | None, transport: str, rec: dict) -> int:
    hub = sim.industrial
    st = hub.ingestion
    meas = env.get("measurements")
    if not meas:
        rec["errors"].append("telemetry requires a non-empty 'measurements' object")
        return 0
    rt = hub.runtime.get(aid)
    if rt is None:
        rec["errors"].append(f"asset '{aid}' defines no sensors")
        return 0
    sync = hub.sync_config(aid)
    by_key = {}
    for s in rt.profile["sensors"]:
        by_key[s["id"]] = s
        by_key.setdefault(s["external_key"], s)
    cycle_id = None
    if env.get("cycle_id"):
        cycle_id = st.cycle_map.get((aid, env["cycle_id"]))
    count = 0
    dirty: set[str] = set()
    for key, raw in meas.items():
        s = by_key.get(key)
        if s is None:
            rec["errors"].append(f"unknown measurement '{key}' for asset '{aid}'")
            continue
        if not s["enabled"]:
            rec["warnings"].append(f"sensor '{s['id']}' is disabled; value ignored")
            continue
        if hub.sensor_source(aid, s) != "external":
            rec["errors"].append(f"sensor '{s['id']}' is simulated; external values are not accepted")
            continue
        try:
            value, unit, mts, quality = _measurement_value(raw, key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and unit:
                value = convert(float(value), unit, s["unit"])
            if s["data_type"] == "boolean" and not isinstance(value, bool):
                raise ValidationError(f"measurement '{key}' must be a boolean")
            if s["data_type"] != "boolean" and (isinstance(value, bool) or not isinstance(value, (int, float))
                                                or not math.isfinite(value)):
                raise ValidationError(f"measurement '{key}' must be a finite number")
        except ValidationError as exc:
            rec["errors"].append(str(exc))
            continue
        src_ts = mts if mts is not None else ts
        sim_time = sim.now
        if src_ts is not None and st.time_epoch is None and sync["time_mode"] == "source":
            st.time_epoch = src_ts - sim.now
        update_latest = True
        if src_ts is not None:
            last = st.last_source_ts.get((aid, s["id"]))
            if sync["time_mode"] == "source" and st.time_epoch is not None:
                sim_time = min(sim.now, max(0.0, src_ts - st.time_epoch))
            if last is not None and src_ts < last:
                quality = "out_of_order"
                update_latest = False
            else:
                st.last_source_ts[(aid, s["id"])] = src_ts
            if st.time_epoch is not None and quality != "out_of_order":
                lateness = sim.now - (src_ts - st.time_epoch)
                if lateness > sync["max_lateness_s"]:
                    quality = "late"
                    update_latest = sync["late_policy"] == "apply"
            if quality in ("late", "out_of_order") and sync["late_policy"] == "discard":
                rec["warnings"].append(f"measurement '{key}' discarded ({quality})")
                st.counts[f"discarded_{quality}"] += 1
                continue
        hub.record_observation(aid, s, value, raw_value=_raw(raw), source=transport, quality=quality,
                               cycle_id=cycle_id, sim_time=sim_time, source_timestamp=_iso(src_ts),
                               message_id=env["event_id"], propagate=False, update_latest=update_latest)
        if update_latest:
            dirty.add(f"sensor.{s['id']}")
        count += 1
    if dirty:
        hub._propagate(aid, dirty, evaluate_conditions=True)
    rec["observations"] = rec.get("observations", 0) + count
    return count


def _raw(raw: Any) -> Any:
    return raw.get("value") if isinstance(raw, dict) else raw


def _measurement_value(raw: Any, key: str) -> tuple[Any, str | None, float | None, str | None]:
    if isinstance(raw, dict):
        if "value" not in raw:
            raise ValidationError(f"measurement '{key}' object requires 'value'")
        unit = raw.get("unit")
        if unit is not None and not isinstance(unit, str):
            raise ValidationError(f"measurement '{key}' unit must be a string")
        q = raw.get("quality")
        if q is not None and q not in ("good", "estimated", "bad", "uncertain"):
            raise ValidationError(f"measurement '{key}' quality must be good/estimated/bad/uncertain")
        return raw["value"], unit, parse_timestamp(raw.get("timestamp"), f"measurements.{key}.timestamp"), q
    return raw, None, None, None


def _resolve_inputs(sim, env: dict) -> list[str] | None:
    st = sim.industrial.ingestion
    items = env.get("inputs")
    if not items:
        return None
    out = []
    for k, item in enumerate(items):
        ref = _product_ref(item)
        if ref is None:
            raise ValidationError(f"inputs[{k}].product_id is required")
        out.append(st.product_map.get(ref, ref))
    return out


def _cycle_started(sim, aid: str, env: dict, ts, transport: str, rec: dict) -> None:
    hub = sim.industrial
    st = hub.ingestion
    asset = sim.get_asset(aid)
    if asset.kind != "transformer":
        raise ValidationError("cycle_started is supported for transformers; producers report batch_produced")
    ext = env.get("cycle_id")
    if not ext:
        raise ValidationError("cycle_started requires cycle_id")
    if (aid, ext) in st.cycle_map:
        rec["status_detail"] = "duplicate cycle start ignored"
        rec["warnings"].append(f"cycle '{ext}' was already started")
        return
    strict = hub.sync_config(aid)["reconciliation"] == "strict"
    blocking = hub.blocking_condition(aid)
    if blocking:
        rec["warnings"].append(f"cycle started while blocking working condition '{blocking}' is active")
    cycle = asset.start_external_cycle(cycle_id=ext, input_ids=_resolve_inputs(sim, env), strict=strict,
                                       timestamp=_iso(ts))
    st.cycle_map[(aid, ext)] = cycle.id
    rec["cycle_id"] = cycle.id
    if cycle.exceptions:
        rec["warnings"].extend(cycle.exceptions)
    if env.get("measurements"):
        _telemetry(sim, aid, env, ts, transport, rec)
    pending = st.pending_completions.pop((aid, ext), None)
    if pending is not None:
        sub = {"errors": [], "warnings": []}
        _cycle_completed(sim, aid, pending, parse_timestamp(pending.get("timestamp")), transport, sub)
        rec["warnings"].append(f"applied pending completion {pending['event_id']}")
        rec["warnings"].extend(sub["warnings"])


def _cycle_completed(sim, aid: str, env: dict, ts, transport: str, rec: dict) -> None:
    hub = sim.industrial
    st = hub.ingestion
    asset = sim.get_asset(aid)
    strict = hub.sync_config(aid)["reconciliation"] == "strict"
    outputs = _outputs(env)
    for spec in outputs:
        if spec.get("product_id") and spec["product_id"] in st.product_map:
            raise ConflictError(f"external product '{spec['product_id']}' already exists")
    ext = env.get("cycle_id")
    if asset.kind == "producer":
        if ext and (aid, ext) in st.cycle_map:
            rec["warnings"].append(f"batch '{ext}' was already recorded")
            return
        if env.get("measurements"):
            _telemetry(sim, aid, env, ts, transport, rec)
        ids = asset.produce_external_batch(cycle_id=ext, outputs=outputs, strict=strict, timestamp=_iso(ts))
        cid = sim.products[ids[0]].origin_cycle_id if ids else None
        if ext and cid:
            st.cycle_map[(aid, ext)] = cid
        _map_products(sim, ids, rec, cid)
        return
    if asset.kind != "transformer":
        raise ValidationError(f"{env['event_type']} is not supported for {asset.kind}s")
    if not ext:
        raise ValidationError("cycle_completed requires cycle_id")
    cid = st.cycle_map.get((aid, ext))
    if cid is None:
        if strict:
            raise ConflictError(f"cycle '{ext}' completed before it was started (strict reconciliation)")
        st.pending_completions[(aid, ext)] = copy.deepcopy(env)
        rec["warnings"].append(f"completion of unknown cycle '{ext}' kept pending until its start arrives")
        rec["status_override"] = "pending"
        return
    cycle = sim.cycles[cid]
    if cycle.status != "active":
        rec["warnings"].append(f"cycle '{ext}' is already {cycle.status}; completion ignored")
        return
    if env.get("measurements"):
        _telemetry(sim, aid, env, ts, transport, rec)
    ids = asset.complete_external_cycle(cycle, outputs=outputs, strict=strict, timestamp=_iso(ts))
    _map_products(sim, ids, rec, cid)
    if cycle.exceptions:
        rec["warnings"].extend(cycle.exceptions)


def _map_products(sim, ids: list[str], rec: dict, cid: str | None) -> None:
    st = sim.industrial.ingestion
    for pid in ids:
        ext = sim.products[pid].external_id
        if ext:
            st.product_map[ext] = pid
    rec["cycle_id"] = cid
    rec["output_product_ids"] = list(ids)


def _cycle_cancelled(sim, aid: str, env: dict, rec: dict) -> None:
    st = sim.industrial.ingestion
    asset = sim.get_asset(aid)
    ext = env.get("cycle_id")
    cid = st.cycle_map.get((aid, ext)) if ext else None
    if cid is None:
        st.pending_completions.pop((aid, ext), None)
        raise NotFoundError(f"unknown external cycle '{ext}'")
    if sim.cycles[cid].status != "active":
        rec["warnings"].append(f"cycle '{ext}' is already {sim.cycles[cid].status}")
        return
    asset.cancel_external_cycle(str(env.get("reason") or "cancelled by external system"))
    rec["cycle_id"] = cid


def _product_updated(sim, aid: str, env: dict, rec: dict) -> None:
    st = sim.industrial.ingestion
    ref = env.get("product_id")
    if not isinstance(ref, str) or not ref:
        raise ValidationError("product_updated requires product_id")
    pid = st.product_map.get(ref, ref)
    p = sim.get_product(pid)
    attrs = env.get("attributes") or {}
    pt = sim.product_types.get(p.product_type_id)
    defs = {a["name"]: a for a in (pt.attributes if pt else [])}
    clean = {}
    for k, v in attrs.items():
        clean[k] = coerce_value(defs[k], v, f"attribute '{k}'") if k in defs else copy.deepcopy(v)
    if "is_defective" in env and not isinstance(env["is_defective"], bool):
        raise ValidationError("is_defective must be a boolean")
    for k, v in clean.items():
        p.attributes[k] = v
        p.attribute_provenance[k] = {"kind": "external", "source_type": "external", "asset_id": aid,
                                     "event_id": env["event_id"], "evaluated_at": sim.now, "status": "ok"}
    if isinstance(env.get("is_defective"), bool):
        p.is_defective = env["is_defective"]
    rec["product_id"] = pid
    asset = sim.get_asset(aid)
    sim.emit("product_updated", asset, output_product_ids=[pid],
             details={"attributes": sorted(clean), "event_id": env["event_id"], "source": "external"})


__all__ = ["ingest", "IngestionState", "parse_timestamp", "EVENT_TYPES", "SCHEMA_VERSION", "MAX_MESSAGE_BYTES"]
