"""Validation/normalisation of industrial definitions.

All definitions are stored as plain JSON-compatible dicts (pickle- and scenario-friendly). The
functions in this module turn user input into canonical dicts or raise :class:`ValidationError`.

Profile sections (per asset type and per asset instance)::

    parameters  [{name, label, type, unit, default, min, max, allowed, overridable, description}]
    sensors     [{id, name, quantity, unit, source, model, noise_std, gain, offset, resolution,
                  min, max, sample_interval_s, filter, external_key, enabled, description}]
    composites  [{id, name, unit, expression, missing, default, description}]
    conditions  [{id, name, severity, priority, activation, deactivation, delay_on_s,
                  delay_off_s, effects, action, message, enabled}]
    behaviors   [{id, name, version, output, expression, when, min, max, enabled, description}]
"""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, Callable

from ..errors import ValidationError
from ..ids import validate_id
from .expressions import compile_expression, validate_expression

SECTIONS = ("parameters", "sensors", "composites", "conditions", "behaviors")
SECTION_KEY = {"parameters": "name", "sensors": "id", "composites": "id", "conditions": "id", "behaviors": "id"}

VALUE_TYPES = ("number", "integer", "boolean", "string", "enum", "json")
SENSOR_DATA_TYPES = ("number", "integer", "boolean")
PARAMETER_BOUNDARIES = ("next_cycle", "immediate")
MAPPING_MISSING = ("default", "skip", "null")
SENSOR_SOURCES = ("auto", "simulated", "external")
SENSOR_MODELS = ("none", "constant", "expression", "first_order", "random_walk")
FILTERS = ("none", "moving_average", "ema")
SEVERITIES = ("info", "warning", "critical")
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
ACTIONS = ("none", "block", "stop", "fault")
EFFECT_OPS = ("multiply", "add", "set", "min", "max")
MISSING_POLICIES = ("skip", "default", "last")

# Cycle features each kind exposes to behaviour models, condition effects and mappings.
FEATURES: dict[str, tuple[str, ...]] = {
    "transformer": ("speed_pct", "time_multiplier", "processing_time_s", "quality_score", "defect_probability"),
    "producer": ("interval_s", "batch_quantity", "defect_probability"),
}
STATE_FLAGS = ("running", "processing", "blocked", "idle", "operational", "stopped", "failed", "unconfigured")
CYCLE_VARS = ("cycle.active", "cycle.progress", "cycle.elapsed_s")

MEASUREMENT_AGGREGATIONS = ("final", "initial", "avg", "min", "max", "latest", "count", "range")
INPUT_AGGREGATIONS = ("first", "last", "avg", "min", "max", "sum", "count", "any", "all", "concat", "unique",
                      "majority", "weighted_avg")
MAPPING_SOURCES = ("constant", "parameter", "measurement", "feature", "input", "expression", "order", "cycle",
                   "condition")
CYCLE_FIELDS = ("cycle_id", "lot_number", "started_at", "ended_at", "duration_s", "asset_id", "asset_name",
                "recipe_id", "source", "external_cycle_id")


# --------------------------------------------------------------------------- primitives

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def ident(value: Any, field: str) -> str:
    """Identifiers usable inside expressions (``sensor.<id>``): letters, digits and '_'."""
    if not isinstance(value, str) or not _IDENT.match(value):
        raise ValidationError(f"invalid {field} {value!r}: use 1-64 letters, digits or '_' (not starting "
                              "with a digit)")
    return value

def _num(v: Any, field: str, *, allow_none: bool = False, minimum: float | None = None,
         exclusive_min: bool = False) -> float | None:
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValidationError(f"{field} must be a finite number")
    if minimum is not None and (v < minimum or (exclusive_min and v == minimum)):
        raise ValidationError(f"{field} must be {'>' if exclusive_min else '>='} {minimum}")
    return float(v)


def _str(v: Any, field: str, *, default: str = "", max_len: int = 500) -> str:
    if v is None:
        return default
    if not isinstance(v, str):
        raise ValidationError(f"{field} must be a string")
    if len(v) > max_len:
        raise ValidationError(f"{field} is too long")
    return v


def _choice(v: Any, field: str, options: tuple, default: Any) -> Any:
    if v is None:
        return default
    if v not in options:
        raise ValidationError(f"{field} must be one of {list(options)}")
    return v


def _obj(v: Any, field: str) -> dict:
    if not isinstance(v, dict):
        raise ValidationError(f"{field} must be an object")
    return v


def _list(v: Any, field: str) -> list:
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValidationError(f"{field} must be a list")
    return v


def coerce_value(defn: dict, value: Any, field: str) -> Any:
    """Validate/coerce ``value`` against a parameter or attribute definition."""
    t = defn.get("type", "number")
    if value is None:
        return None
    if t in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValidationError(f"{field} must be a number")
        if t == "integer":
            if float(value) != int(value):
                raise ValidationError(f"{field} must be an integer")
            value = int(value)
        else:
            value = float(value)
        lo, hi = defn.get("min"), defn.get("max")
        if lo is not None and value < lo:
            raise ValidationError(f"{field}={value} is below the minimum {lo}")
        if hi is not None and value > hi:
            raise ValidationError(f"{field}={value} is above the maximum {hi}")
        return value
    if t == "boolean":
        if not isinstance(value, bool):
            raise ValidationError(f"{field} must be true or false")
        return value
    if t == "json":
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{field} must be JSON-serialisable") from None
        return copy.deepcopy(value)
    if t == "enum":
        if value not in (defn.get("allowed") or []):
            raise ValidationError(f"{field} must be one of {defn.get('allowed')}")
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    allowed = defn.get("allowed")
    if allowed and value not in allowed:
        raise ValidationError(f"{field} must be one of {allowed}")
    return value


def _value_definition(d: dict, field: str, key: str) -> dict:
    t = _choice(d.get("type"), f"{field}.type", VALUE_TYPES, "number")
    out = {key: ident(d.get(key), f"{field}.{key}"),
           "label": _str(d.get("label"), f"{field}.label"),
           "type": t, "unit": _str(d.get("unit"), f"{field}.unit", max_len=40),
           "description": _str(d.get("description"), f"{field}.description"),
           "min": _num(d.get("min"), f"{field}.min", allow_none=True) if t in ("number", "integer") else None,
           "max": _num(d.get("max"), f"{field}.max", allow_none=True) if t in ("number", "integer") else None,
           "allowed": None}
    if out["min"] is not None and out["max"] is not None and out["min"] > out["max"]:
        raise ValidationError(f"{field}: min must not exceed max")
    allowed = d.get("allowed")
    if t == "enum" or (t == "string" and allowed):
        allowed = _list(allowed, f"{field}.allowed")
        if t == "enum" and not allowed:
            raise ValidationError(f"{field}: enum definitions need a non-empty 'allowed' list")
        out["allowed"] = list(allowed)
    return out


# --------------------------------------------------------------------------- profile items

def normalize_parameter(d: Any, field: str = "parameter") -> dict:
    d = _obj(d, field)
    out = _value_definition(d, field, "name")
    out["overridable"] = bool(d.get("overridable", True))
    out["effective"] = _choice(d.get("effective"), f"{field}.effective", PARAMETER_BOUNDARIES, "next_cycle")
    out["default"] = coerce_value(out, d.get("default"), f"{field}.default")
    if out["default"] is None:
        raise ValidationError(f"{field}.default is required")
    return out


def normalize_sensor(d: Any, field: str = "sensor") -> dict:
    d = _obj(d, field)
    model = d.get("model") or {"type": "none"}
    model = _obj(model, f"{field}.model")
    mtype = _choice(model.get("type"), f"{field}.model.type", SENSOR_MODELS, "none")
    m: dict[str, Any] = {"type": mtype}
    if mtype == "constant":
        m["value"] = _num(model.get("value", 0.0), f"{field}.model.value")
    elif mtype == "expression":
        m["expression"] = validate_expression(model.get("expression"), f"{field}.model.expression")
    elif mtype == "first_order":
        m["target"] = validate_expression(model.get("target"), f"{field}.model.target")
        m["time_constant_s"] = _num(model.get("time_constant_s", 10.0), f"{field}.model.time_constant_s",
                                    minimum=0, exclusive_min=True)
        m["initial"] = _num(model.get("initial", 0.0), f"{field}.model.initial")
    elif mtype == "random_walk":
        m["initial"] = _num(model.get("initial", 0.0), f"{field}.model.initial")
        m["step_std"] = _num(model.get("step_std", 1.0), f"{field}.model.step_std", minimum=0)
    filt = _obj(d.get("filter") or {"type": "none"}, f"{field}.filter")
    ftype = _choice(filt.get("type"), f"{field}.filter.type", FILTERS, "none")
    f: dict[str, Any] = {"type": ftype}
    if ftype == "moving_average":
        w = filt.get("window", 5)
        if isinstance(w, bool) or not isinstance(w, int) or not 1 <= w <= 1000:
            raise ValidationError(f"{field}.filter.window must be an integer 1..1000")
        f["window"] = w
    elif ftype == "ema":
        a = _num(filt.get("alpha", 0.3), f"{field}.filter.alpha")
        if not 0 < a <= 1:
            raise ValidationError(f"{field}.filter.alpha must be within (0, 1]")
        f["alpha"] = a
    sid = ident(d.get("id"), f"{field}.id")
    out = {
        "id": sid, "name": _str(d.get("name"), f"{field}.name", default=sid) or sid,
        "quantity": _str(d.get("quantity"), f"{field}.quantity", max_len=60),
        "data_type": _choice(d.get("data_type"), f"{field}.data_type", SENSOR_DATA_TYPES, "number"),
        "unit": _str(d.get("unit"), f"{field}.unit", max_len=40),
        "source": _choice(d.get("source"), f"{field}.source", SENSOR_SOURCES, "auto"),
        "model": m,
        "noise_std": _num(d.get("noise_std", 0.0), f"{field}.noise_std", minimum=0),
        "gain": _num(d.get("gain", 1.0), f"{field}.gain"),
        "offset": _num(d.get("offset", 0.0), f"{field}.offset"),
        "resolution": _num(d.get("resolution"), f"{field}.resolution", allow_none=True, minimum=0,
                           exclusive_min=True),
        "min": _num(d.get("min"), f"{field}.min", allow_none=True),
        "max": _num(d.get("max"), f"{field}.max", allow_none=True),
        "sample_interval_s": _num(d.get("sample_interval_s", 1.0), f"{field}.sample_interval_s", minimum=0.001),
        "filter": f,
        "external_key": _str(d.get("external_key"), f"{field}.external_key", default=sid, max_len=120) or sid,
        "enabled": bool(d.get("enabled", True)),
        "description": _str(d.get("description"), f"{field}.description"),
    }
    if out["min"] is not None and out["max"] is not None and out["min"] > out["max"]:
        raise ValidationError(f"{field}: min must not exceed max")
    return out


def normalize_composite(d: Any, field: str = "composite") -> dict:
    d = _obj(d, field)
    cid = ident(d.get("id"), f"{field}.id")
    return {
        "id": cid, "name": _str(d.get("name"), f"{field}.name", default=cid) or cid,
        "unit": _str(d.get("unit"), f"{field}.unit", max_len=40),
        "expression": validate_expression(d.get("expression"), f"{field}.expression"),
        "missing": _choice(d.get("missing"), f"{field}.missing", MISSING_POLICIES, "skip"),
        "default": _num(d.get("default"), f"{field}.default", allow_none=True),
        "description": _str(d.get("description"), f"{field}.description"),
    }


def normalize_effect(d: Any, field: str) -> dict:
    d = _obj(d, field)
    target = ident(d.get("target"), f"{field}.target")
    op = _choice(d.get("op"), f"{field}.op", EFFECT_OPS, "multiply")
    value = d.get("value")
    if isinstance(value, str):
        value = validate_expression(value, f"{field}.value")
    else:
        value = _num(value, f"{field}.value")
    return {"target": target, "op": op, "value": value}


def normalize_condition(d: Any, field: str = "condition") -> dict:
    d = _obj(d, field)
    cid = ident(d.get("id"), f"{field}.id")
    prio = d.get("priority", 0)
    if isinstance(prio, bool) or not isinstance(prio, int):
        raise ValidationError(f"{field}.priority must be an integer")
    deact = d.get("deactivation")
    return {
        "id": cid, "name": _str(d.get("name"), f"{field}.name", default=cid) or cid,
        "severity": _choice(d.get("severity"), f"{field}.severity", SEVERITIES, "warning"),
        "priority": prio,
        "activation": validate_expression(d.get("activation"), f"{field}.activation"),
        "deactivation": None if deact in (None, "") else validate_expression(deact, f"{field}.deactivation"),
        "delay_on_s": _num(d.get("delay_on_s", 0.0), f"{field}.delay_on_s", minimum=0),
        "delay_off_s": _num(d.get("delay_off_s", 0.0), f"{field}.delay_off_s", minimum=0),
        "effects": [normalize_effect(e, f"{field}.effects[{i}]") for i, e in enumerate(_list(d.get("effects"),
                                                                                              f"{field}.effects"))],
        "action": _choice(d.get("action"), f"{field}.action", ACTIONS, "none"),
        "description": _str(d.get("description"), f"{field}.description"),
        "message": _str(d.get("message"), f"{field}.message"),
        "enabled": bool(d.get("enabled", True)),
    }


def normalize_behavior(d: Any, field: str = "behavior") -> dict:
    d = _obj(d, field)
    bid = ident(d.get("id"), f"{field}.id")
    version = d.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, (int, str)) or str(version) == "":
        raise ValidationError(f"{field}.version must be an integer or string")
    when = d.get("when")
    out = {
        "id": bid, "name": _str(d.get("name"), f"{field}.name", default=bid) or bid,
        "version": version,
        "output": ident(d.get("output"), f"{field}.output"),
        "expression": validate_expression(d.get("expression"), f"{field}.expression"),
        "when": None if when in (None, "") else validate_expression(when, f"{field}.when"),
        "min": _num(d.get("min"), f"{field}.min", allow_none=True),
        "max": _num(d.get("max"), f"{field}.max", allow_none=True),
        "enabled": bool(d.get("enabled", True)),
        "description": _str(d.get("description"), f"{field}.description"),
        "effective_boundary": "cycle_start",
        "input_ranges": {},
    }
    if out["min"] is not None and out["max"] is not None and out["min"] > out["max"]:
        raise ValidationError(f"{field}: min must not exceed max")
    ranges = _obj(d.get("input_ranges") or {}, f"{field}.input_ranges")
    for var, rng in ranges.items():
        if not isinstance(rng, (list, tuple)) or len(rng) != 2:
            raise ValidationError(f"{field}.input_ranges.{var} must be [min, max]")
        lo = _num(rng[0], f"{field}.input_ranges.{var}[0]", allow_none=True)
        hi = _num(rng[1], f"{field}.input_ranges.{var}[1]", allow_none=True)
        if lo is not None and hi is not None and lo > hi:
            raise ValidationError(f"{field}.input_ranges.{var}: min must not exceed max")
        out["input_ranges"][str(var)] = [lo, hi]
    out["inputs"] = sorted(compile_expression(out["expression"]).variables
                           | (compile_expression(out["when"]).variables if out["when"] else frozenset()))
    return out


NORMALIZERS: dict[str, Callable[[Any, str], dict]] = {
    "parameters": normalize_parameter, "sensors": normalize_sensor, "composites": normalize_composite,
    "conditions": normalize_condition, "behaviors": normalize_behavior,
}


def empty_profile() -> dict:
    return {s: [] for s in SECTIONS}


def normalize_profile(raw: Any, field: str = "profile") -> dict:
    """Normalise every section (structure only; references are checked by :func:`check_profile`)."""
    if raw is None:
        return empty_profile()
    raw = _obj(raw, field)
    unknown = set(raw) - set(SECTIONS)
    if unknown:
        raise ValidationError(f"{field}: unknown section(s) {sorted(unknown)}")
    out = {}
    for s in SECTIONS:
        items = [NORMALIZERS[s](it, f"{field}.{s}[{i}]") for i, it in enumerate(_list(raw.get(s), f"{field}.{s}"))]
        key = SECTION_KEY[s]
        seen: set[str] = set()
        for it in items:
            if it[key] in seen:
                raise ValidationError(f"{field}.{s}: duplicate {key} '{it[key]}'")
            seen.add(it[key])
        out[s] = items
    return out


def profile_is_empty(profile: dict) -> bool:
    return not any(profile.get(s) for s in SECTIONS)


# --------------------------------------------------------------------------- reference checks

def variable_checker(profile: dict, kind: str, *, extra: Callable[[str], bool] | None = None) -> Callable[[str], bool]:
    params = {p["name"] for p in profile["parameters"]}
    sensors = {s["id"] for s in profile["sensors"]}
    composites = {c["id"] for c in profile["composites"]}
    conditions = {c["id"] for c in profile["conditions"]}
    features = set(FEATURES.get(kind, ()))

    def allowed(v: str) -> bool:
        head, _, rest = v.partition(".")
        if v in ("t", "self", "dt") or v in CYCLE_VARS:
            return True
        if head == "param":
            return rest in params
        if head == "sensor":
            return rest in sensors
        if head == "composite":
            return rest in composites
        if head == "feature":
            return rest in features
        if head == "state":
            return rest in STATE_FLAGS
        if head == "condition":
            return rest in conditions
        if head == "recipe":
            return bool(rest)
        return bool(extra and extra(v))

    return allowed


def composite_order(composites: list[dict]) -> list[str]:
    """Topological evaluation order of composites; raises ValidationError on dependency cycles."""
    ids = {c["id"] for c in composites}
    deps = {c["id"]: sorted({v.split(".", 1)[1] for v in compile_expression(c["expression"]).variables
                             if v.startswith("composite.") and v.split(".", 1)[1] in ids}) for c in composites}
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        st = state.get(n, 0)
        if st == 2:
            return
        if st == 1:
            cyc = path[path.index(n):] + [n]
            raise ValidationError(f"composite measurement cycle detected: {' -> '.join(cyc)}")
        state[n] = 1
        for dep in deps[n]:
            visit(dep, path + [n])
        state[n] = 2
        order.append(n)

    for c in composites:
        visit(c["id"], [])
    return order


def check_profile(profile: dict, kind: str) -> None:
    """Cross-reference validation of an *effective* profile for an asset of ``kind``."""
    sensor_ids = {s["id"] for s in profile["sensors"]}
    for c in profile["composites"]:
        if c["id"] in sensor_ids:
            raise ValidationError(f"composite '{c['id']}' clashes with a sensor of the same id")
    allowed = variable_checker(profile, kind)
    features = FEATURES.get(kind, ())
    for s in profile["sensors"]:
        m = s["model"]
        for key in ("expression", "target"):
            if key in m:
                validate_expression(m[key], f"sensor '{s['id']}' model.{key}", allowed)
    for c in profile["composites"]:
        validate_expression(c["expression"], f"composite '{c['id']}'", allowed)
    composite_order(profile["composites"])
    for c in profile["conditions"]:
        validate_expression(c["activation"], f"condition '{c['id']}' activation", allowed)
        if c["deactivation"]:
            validate_expression(c["deactivation"], f"condition '{c['id']}' deactivation", allowed)
        for e in c["effects"]:
            if e["target"] not in features:
                raise ValidationError(f"condition '{c['id']}' effect targets unknown feature '{e['target']}' "
                                      f"(available for {kind}: {list(features)})")
            if isinstance(e["value"], str):
                validate_expression(e["value"], f"condition '{c['id']}' effect value", allowed)
        if c["action"] == "block" and kind not in FEATURES:
            raise ValidationError(f"condition '{c['id']}': action 'block' only applies to producers and transformers")
    for b in profile["behaviors"]:
        if b["output"] not in features:
            raise ValidationError(f"behavior '{b['id']}' outputs unknown feature '{b['output']}' "
                                  f"(available for {kind}: {list(features)})")
        validate_expression(b["expression"], f"behavior '{b['id']}'", allowed)
        if b["when"]:
            validate_expression(b["when"], f"behavior '{b['id']}' when", allowed)


# --------------------------------------------------------------------------- overrides

def apply_item_override(section: str, item: dict, patch: dict, field: str) -> dict:
    key = SECTION_KEY[section]
    if key in patch and patch[key] != item[key]:
        raise ValidationError(f"{field}: the {key} of a template item cannot be overridden")
    merged = copy.deepcopy(item)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = copy.deepcopy(v)
    return NORMALIZERS[section](merged, field)


# --------------------------------------------------------------------------- product attributes

def normalize_attribute_definition(d: Any, field: str = "attribute") -> dict:
    d = _obj(d, field)
    out = _value_definition(d, field, "name")
    out["required"] = bool(d.get("required", False))
    out["default"] = coerce_value(out, d.get("default"), f"{field}.default")
    return out


def normalize_attribute_definitions(raw: Any, field: str = "attributes") -> list[dict]:
    items = [normalize_attribute_definition(a, f"{field}[{i}]") for i, a in enumerate(_list(raw, field))]
    names = [a["name"] for a in items]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ValidationError(f"{field}: duplicate attribute name(s) {sorted(dup)}")
    return items


def normalize_mapping(d: Any, field: str = "mapping") -> dict:
    d = _obj(d, field)
    source = _choice(d.get("source"), f"{field}.source", MAPPING_SOURCES, "constant")
    out: dict[str, Any] = {"attribute": ident(d.get("attribute"), f"{field}.attribute"), "source": source}
    if source == "constant":
        if "value" not in d:
            raise ValidationError(f"{field}: constant mappings need a 'value'")
        v = d["value"]
        if v is not None and not isinstance(v, (str, int, float, bool)):
            raise ValidationError(f"{field}.value must be a scalar")
        out["value"] = v
    elif source == "expression":
        out["expression"] = validate_expression(d.get("expression"), f"{field}.expression")
    else:
        out["ref"] = validate_id(d.get("ref"), f"{field}.ref") if source != "cycle" else \
            _choice(d.get("ref"), f"{field}.ref", CYCLE_FIELDS, None)
        if out["ref"] is None:
            raise ValidationError(f"{field}.ref is required")
    if source == "measurement":
        out["aggregation"] = _choice(d.get("aggregation"), f"{field}.aggregation", MEASUREMENT_AGGREGATIONS, "final")
    elif source == "input":
        out["aggregation"] = _choice(d.get("aggregation"), f"{field}.aggregation", INPUT_AGGREGATIONS, "first")
        slot = d.get("input_slot")
        if slot is not None and (isinstance(slot, bool) or not isinstance(slot, int) or slot < 0):
            raise ValidationError(f"{field}.input_slot must be a non-negative integer or null")
        out["input_slot"] = slot
        ptid = d.get("input_product_type_id")
        out["input_product_type_id"] = validate_id(ptid, f"{field}.input_product_type_id") if ptid else None
        if out["aggregation"] == "weighted_avg":
            out["weight_attribute"] = ident(d.get("weight_attribute"), f"{field}.weight_attribute")
    when = d.get("when")
    out["when"] = None if when in (None, "") else validate_expression(when, f"{field}.when")
    out["on_missing"] = _choice(d.get("on_missing"), f"{field}.on_missing", MAPPING_MISSING, "default")
    out["label"] = _str(d.get("label"), f"{field}.label")
    out["scale"] = _num(d.get("scale", 1.0), f"{field}.scale")
    out["offset"] = _num(d.get("offset", 0.0), f"{field}.offset")
    out["default"] = d.get("default")
    if out["default"] is not None and not isinstance(out["default"], (str, int, float, bool)):
        raise ValidationError(f"{field}.default must be a scalar")
    return out


def normalize_mappings(raw: Any, field: str = "attribute_mappings") -> list[dict]:
    items = [normalize_mapping(m, f"{field}[{i}]") for i, m in enumerate(_list(raw, field))]
    names = [m["attribute"] for m in items]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ValidationError(f"{field}: attribute(s) mapped more than once: {sorted(dup)}")
    return items


def check_mappings_against_type(mappings: list[dict], attr_defs: list[dict], field: str) -> None:
    if not attr_defs:
        return
    known = {a["name"] for a in attr_defs}
    for m in mappings:
        if m["attribute"] not in known:
            raise ValidationError(f"{field}: attribute '{m['attribute']}' is not defined on the product type "
                                  f"(defined: {sorted(known)})")
