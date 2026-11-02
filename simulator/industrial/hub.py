"""Industrial hub: asset types, instance profiles, sensing, working conditions and behaviour models.

The hub is owned by the :class:`~simulator.engine.Simulation` (``sim.industrial``) so that its
state is pickled with checkpoints and reset together with the simulation. The engine and the
asset implementations call into the hub at well-defined boundaries only:

* scheduler events ``ind:sample`` (periodic simulated sensor sampling), ``ind:cond`` (delayed
  condition transitions) and ``ind:eval`` (deferred condition evaluation),
* cycle start (:meth:`cycle_features`, :meth:`on_cycle_started`) and cycle completion
  (:meth:`on_cycle_completed`, :meth:`resolve_attributes`),
* gating (:meth:`blocking_condition`, :meth:`production_external`).

Assets without an industrial profile never reach any of this code, so the original simulator
behaviour (and determinism) is unchanged for them.
"""

from __future__ import annotations

import copy
import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..errors import ConflictError, NotFoundError, ValidationError
from .asset_types import (OVERRIDE_SECTIONS, TYPE_KINDS, AssetBinding, AssetType, diff_profiles,
                          effective_profile, empty_overrides, parameter_values, validate_parameter_values)
from .definitions import (FEATURES, SECTION_KEY, SECTIONS, SEVERITY_RANK, check_profile, coerce_value,
                          composite_order, empty_profile, normalize_profile, profile_is_empty)
from .expressions import EvaluationError, MissingValue, as_number, compile_expression
from .measurements import MeasurementStore, Observation, SensorRuntime

SAMPLE_PRIORITY = 35

CONFIG_KEYS: dict[str, set[str]] = {
    "producer": {"product_type_id", "batch_quantity", "interval", "interval_unit", "max_pending_batches",
                 "production_order_id", "defect_probability", "start_delay_s", "attributes",
                 "attribute_mappings", "initial_state"},
    "transformer": {"recipe_id", "speed_pct", "input_buffer_multiplier", "output_buffer_multiplier",
                    "quality_model", "speed_model", "initial_state"},
    "conveyor": {"length_m", "capacity_per_m", "speed_m_s", "initial_state"},
    "splitter": {"mode", "percentage_a", "rules", "fallback_branch", "buffer_capacity", "initial_state"},
    "sink": {"accepted_product_type_ids", "capacity", "completion_criteria", "initial_state"},
}

SYNC_MODES = ("simulation", "external", "hybrid")
SYNC_CATEGORIES = ("production", "measurements", "configuration", "state")
SYNC_SOURCES = ("simulation", "external")
RECONCILIATION = ("strict", "lenient")
LATE_POLICIES = ("store", "apply", "discard")
TIME_MODES = ("arrival", "source")

FEATURE_BOUNDS = {"speed_pct": (1.0, 100.0), "time_multiplier": (1e-3, None), "processing_time_s": (1e-3, None),
                  "quality_score": (0.0, 1.0), "defect_probability": (0.0, 1.0), "interval_s": (1e-3, None),
                  "batch_quantity": (1.0, 10_000.0)}


def default_sync() -> dict:
    return {"mode": "simulation", "categories": {c: "simulation" for c in SYNC_CATEGORIES}, "external_id": None,
            "reconciliation": "lenient", "late_policy": "store", "time_mode": "arrival", "max_lateness_s": 30.0}


def normalize_sync(raw: Any, current: dict | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError("sync configuration must be an object")
    base = copy.deepcopy(current or default_sync())
    unknown = set(raw) - set(base)
    if unknown:
        raise ValidationError(f"unknown sync field(s) {sorted(unknown)}")
    out = {**base, **{k: copy.deepcopy(v) for k, v in raw.items() if k != "categories"}}
    if out["mode"] not in SYNC_MODES:
        raise ValidationError(f"sync mode must be one of {list(SYNC_MODES)}")
    cats = dict(base["categories"])
    if "categories" in raw:
        if not isinstance(raw["categories"], dict):
            raise ValidationError("sync categories must be an object")
        for k, v in raw["categories"].items():
            if k not in SYNC_CATEGORIES:
                raise ValidationError(f"unknown sync category '{k}' (expected {list(SYNC_CATEGORIES)})")
            if v not in SYNC_SOURCES:
                raise ValidationError(f"sync category '{k}' must be one of {list(SYNC_SOURCES)}")
            cats[k] = v
    if out["mode"] == "simulation":
        cats = {c: "simulation" for c in SYNC_CATEGORIES}
    elif out["mode"] == "external":
        cats = {c: "external" for c in SYNC_CATEGORIES}
    out["categories"] = cats
    ext = out.get("external_id")
    if ext is not None and (not isinstance(ext, str) or not ext.strip() or len(ext) > 200):
        raise ValidationError("external_id must be a non-empty string or null")
    if out["reconciliation"] not in RECONCILIATION:
        raise ValidationError(f"reconciliation must be one of {list(RECONCILIATION)}")
    if out["late_policy"] not in LATE_POLICIES:
        raise ValidationError(f"late_policy must be one of {list(LATE_POLICIES)}")
    if out["time_mode"] not in TIME_MODES:
        raise ValidationError(f"time_mode must be one of {list(TIME_MODES)}")
    ml = out["max_lateness_s"]
    if isinstance(ml, bool) or not isinstance(ml, (int, float)) or ml < 0:
        raise ValidationError("max_lateness_s must be a number >= 0")
    out["max_lateness_s"] = float(ml)
    return out


@dataclass
class AssetRuntime:
    profile: dict = field(default_factory=empty_profile)
    problems: list = field(default_factory=list)
    params: dict = field(default_factory=dict)
    order: list = field(default_factory=list)
    sensors: dict = field(default_factory=dict)          # sid -> SensorRuntime
    sample_events: dict = field(default_factory=dict)    # sid -> scheduler seq
    composite_values: dict = field(default_factory=dict)
    conditions: dict = field(default_factory=dict)       # cid -> state dict
    cond_events: dict = field(default_factory=dict)      # cid -> scheduler seq
    eval_pending: bool = False
    errors: deque = field(default_factory=lambda: deque(maxlen=50))
    last_features: dict | None = None

    def defs(self, section: str) -> dict:
        key = SECTION_KEY[section]
        return {it[key]: it for it in self.profile[section]}


class _Env:
    """Lazy variable resolver for expressions evaluated in the context of one asset."""

    __slots__ = ("hub", "aid", "rt", "extra", "features")

    def __init__(self, hub: "IndustrialHub", aid: str, rt: AssetRuntime, extra: dict | None = None,
                 features: dict | None = None) -> None:
        self.hub, self.aid, self.rt, self.extra, self.features = hub, aid, rt, extra, features

    def get(self, name: str, default: Any = None) -> Any:
        if self.extra is not None and name in self.extra:
            return self.extra[name]
        return self.hub._resolve(self.aid, self.rt, name, self.features)


class _MappingEnv(_Env):
    __slots__ = ("ctx",)

    def __init__(self, hub, aid, rt, ctx: dict, features: dict | None) -> None:
        super().__init__(hub, aid, rt, None, features)
        self.ctx = ctx

    def get(self, name: str, default: Any = None) -> Any:
        head, _, rest = name.partition(".")
        ctx = self.ctx
        if head == "attr":
            return ctx["attrs"].get(rest)
        if head == "input":
            vals = [v for v in (self.hub._input_attr(p, rest) for p in ctx.get("inputs") or []) if v is not None]
            if not vals:
                return None
            nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
            return sum(nums) / len(nums) if len(nums) == len(vals) else vals[0]
        if head == "measure":
            mid, _, agg = rest.partition(".")
            return self.hub._cycle_measure(self.aid, ctx.get("cycle"), mid, agg or "final")[0]
        if head == "cycle" and rest in ("cycle_id", "lot_number", "started_at", "ended_at", "duration_s",
                                        "asset_id", "asset_name", "recipe_id", "source", "external_cycle_id",
                                        "output_slot", "unit_index"):
            return self.hub._cycle_field(ctx, rest)
        if head == "output":
            return {"slot": ctx.get("slot"), "is_byproduct": ctx.get("is_byproduct"),
                    "product_type_id": ctx.get("product_type_id")}.get(rest)
        if rest and head == "sensor" and ctx.get("cycle") is not None:
            v = self.hub._cycle_measure(self.aid, ctx["cycle"], rest, "final")[0]
            if v is not None:
                return v
        if rest and head == "composite" and ctx.get("cycle") is not None:
            v = self.hub._cycle_measure(self.aid, ctx["cycle"], rest, "final")[0]
            if v is not None:
                return v
        return super().get(name, default)


class IndustrialHub:
    def __init__(self, sim) -> None:
        self.sim = sim
        self.asset_types: dict[str, AssetType] = {}
        self.bindings: dict[str, AssetBinding] = {}
        self.sync: dict[str, dict] = {}
        self.runtime: dict[str, AssetRuntime] = {}
        self.store = MeasurementStore()
        self.rng = random.Random(f"sense-{sim.seed}")
        self.transitions: deque[dict] = deque(maxlen=5000)
        self.stats = {"samples": 0, "composites": 0, "transitions": 0, "behaviors_applied": 0,
                      "mapping_errors": 0, "evaluation_errors": 0}
        from .ingestion import IngestionState
        self.ingestion = IngestionState()

    # ================================================================== asset types
    def _validate_type(self, t: AssetType) -> None:
        unknown = set(t.config) - CONFIG_KEYS[t.kind]
        if unknown:
            raise ValidationError(f"unknown configuration field(s) for {t.kind}: {sorted(unknown)}")
        from ..assets import ASSET_KINDS, Conveyor
        cls = Conveyor if t.kind == "conveyor" else ASSET_KINDS[t.kind]
        raw = dict(t.config)
        if t.kind == "producer" and "product_type_id" not in raw:
            if not self.sim.product_types:
                raw = None
            else:
                raw["product_type_id"] = next(iter(self.sim.product_types))
        if raw is not None:
            try:
                cls.parse_config(raw, self.sim)
            except ValidationError as exc:
                raise ValidationError(f"asset type configuration: {exc}") from None
        check_profile(t.profile, t.kind)

    def create_type(self, data: dict) -> AssetType:
        t = AssetType.from_dict({**(data or {}), "version": 1})
        if t.id in self.asset_types:
            raise ConflictError(f"asset type '{t.id}' already exists")
        self._validate_type(t)
        t.updated_at = self.sim.now
        t.changelog = [{"version": 1, "time": self.sim.now, "summary": "created"}]
        self.asset_types[t.id] = t
        return t

    def get_type(self, type_id: str) -> AssetType:
        try:
            return self.asset_types[type_id]
        except KeyError:
            raise NotFoundError(f"unknown asset type '{type_id}'") from None

    def update_type(self, type_id: str, data: dict) -> AssetType:
        cur = self.get_type(type_id)
        if not isinstance(data, dict):
            raise ValidationError("asset type patch must be an object")
        if "id" in data and data["id"] != type_id:
            raise ValidationError("asset type IDs are immutable")
        if "kind" in data and data["kind"] != cur.kind:
            raise ValidationError("the kind of an asset type cannot change")
        merged = {**cur.to_dict(), **{k: v for k, v in data.items() if k not in ("version", "changelog", "change_note")}}
        t = AssetType.from_dict(merged)
        self._validate_type(t)
        changed = t.config != cur.config or t.profile != cur.profile
        t.version = cur.version + (1 if changed else 0)
        t.updated_at = self.sim.now
        t.changelog = list(cur.changelog)
        if changed:
            diff = diff_profiles(cur.profile, t.profile)
            cfg = sorted(k for k in set(cur.config) | set(t.config) if cur.config.get(k) != t.config.get(k))
            t.changelog.append({"version": t.version, "time": self.sim.now,
                                "summary": str(data.get("change_note") or "updated"),
                                "config_keys": cfg, "profile": {s: {k: len(v) for k, v in d.items()}
                                                                for s, d in diff.items()}})
        self.asset_types[type_id] = t
        self.sim._record_change(f"asset-type:{type_id}", {"version": cur.version}, {"version": t.version}, t.version,
                                "explicit synchronisation of each instance (instances keep their snapshot)")
        return t

    def duplicate_type(self, type_id: str, new_id: str, name: str | None = None) -> AssetType:
        src = self.get_type(type_id)
        d = src.to_dict()
        d.update({"id": new_id, "name": name or f"{src.name} (copy)"})
        return self.create_type(d)

    def delete_type(self, type_id: str) -> None:
        self.get_type(type_id)
        users = sorted(a for a, b in self.bindings.items() if b.type_id == type_id)
        if users:
            raise ConflictError(f"asset type '{type_id}' is deployed on {', '.join(users)}; detach them first")
        del self.asset_types[type_id]

    def type_summary(self, t: AssetType) -> dict:
        inst = [a for a, b in self.bindings.items() if b.type_id == t.id]
        outdated = [a for a in inst if (self.bindings[a].type_version or 0) < t.version]
        from ..assets import ASSET_KINDS, Conveyor
        cls = Conveyor if t.kind == "conveyor" else ASSET_KINDS[t.kind]
        return {**t.to_dict(), "instances": sorted(inst), "outdated_instances": sorted(outdated),
                "ports": {"inputs": list(cls.input_port_names), "outputs": list(cls.output_port_names)},
                "features": list(FEATURES.get(t.kind, ()))}

    # ================================================================== bindings / instances
    def binding(self, asset_id: str) -> AssetBinding:
        self.sim.get_asset(asset_id)
        b = self.bindings.get(asset_id)
        return b if b is not None else AssetBinding()

    def deploy(self, type_id: str, name: str, *, config: dict | None = None, asset_id: str | None = None,
               position: dict | None = None, overrides: dict | None = None, local: dict | None = None,
               source: dict | None = None, target: dict | None = None):
        """Create an asset instance from a type: template config + instance config overrides."""
        t = self.get_type(type_id)
        cfg = {**copy.deepcopy(t.config), **copy.deepcopy(config or {})}
        if t.kind == "conveyor":
            asset = self.sim.add_conveyor(name, source or {}, target or {}, cfg, asset_id=asset_id, position=position)
        else:
            asset = self.sim.add_asset(t.kind, name, cfg, asset_id=asset_id, position=position)
        try:
            b = AssetBinding(type_id=t.id, type_version=t.version, template=t.template())
            ov = empty_overrides()
            for k, v in (overrides or {}).items():
                if k not in ov or not isinstance(v, dict):
                    raise ValidationError(f"unknown or invalid override section '{k}'")
                ov[k] = copy.deepcopy(v)
            b.overrides = ov
            b.local = normalize_profile(local, "local")
            self._track_config_overrides(asset, b)
            self._commit(asset, b, reason=f"deployed from asset type {t.id} v{t.version}", initial=True)
        except Exception:
            self.sim.remove_asset(asset.id)
            raise
        return asset

    def attach_type(self, asset_id: str, type_id: str, *, apply_config: bool = True) -> dict:
        """Bind an existing instance to a type. Template config is applied unless ``apply_config`` is False.

        Instance-local items whose key also exists in the template are replaced by the template item."""
        asset = self.sim.get_asset(asset_id)
        t = self.get_type(type_id)
        if t.kind != asset.kind:
            raise ValidationError(f"asset type '{type_id}' is for {t.kind}s, not {asset.kind}s")
        cur = self.binding(asset_id)
        local = copy.deepcopy(cur.local)
        for s in SECTIONS:
            keys = {it[SECTION_KEY[s]] for it in t.profile[s]}
            local[s] = [it for it in local[s] if it[SECTION_KEY[s]] not in keys]
        b = AssetBinding(type_id=t.id, type_version=t.version, template=t.template(), overrides=empty_overrides(),
                         local=local)
        b.overrides["parameters"] = copy.deepcopy(cur.overrides.get("parameters", {}))
        if apply_config:
            patch = {k: v for k, v in t.config.items() if asset.config.get(k) != v}
            if patch:
                self.sim.update_config(asset_id, patch, origin="template")
        self._track_config_overrides(asset, b)
        self._commit(asset, b, reason=f"bound to asset type {t.id} v{t.version}")
        return self.describe(asset_id)

    def detach_type(self, asset_id: str) -> dict:
        """Remove the type binding; the effective profile becomes instance-local."""
        asset = self.sim.get_asset(asset_id)
        cur = self.bindings.get(asset_id)
        if cur is None or cur.type_id is None:
            raise ConflictError(f"asset '{asset_id}' is not bound to an asset type")
        prof, _ = effective_profile(cur)
        b = AssetBinding(local=prof)
        b.overrides["parameters"] = {k: v for k, v in cur.overrides.get("parameters", {}).items()
                                     if any(p["name"] == k for p in prof["parameters"])}
        self._commit(asset, b, reason=f"detached from asset type {cur.type_id}")
        return self.describe(asset_id)

    def restore_binding(self, asset_id: str, data: dict | None, sync: dict | None = None) -> None:
        """Scenario loading: restore a binding without re-applying configuration."""
        asset = self.sim.get_asset(asset_id)
        if data:
            b = AssetBinding.from_dict(data)
            if b.type_id is not None and b.type_id not in self.asset_types:
                raise ValidationError(f"asset '{asset_id}' references unknown asset type '{b.type_id}'")
            self._track_config_overrides(asset, b, keep_existing=True)
            self._commit(asset, b, reason=None, initial=True)
        if sync:
            self.set_sync(asset_id, sync, record=False)

    def _track_config_overrides(self, asset, b: AssetBinding, keep_existing: bool = False) -> None:
        tpl = b.template["config"]
        if not tpl:
            b.resolved_template_config = {}
            return
        try:
            parsed = type(asset).parse_config({**asset.config, **copy.deepcopy(tpl)}, self.sim)
            b.resolved_template_config = {k: parsed[k] for k in tpl if k in parsed}
        except ValidationError:
            b.resolved_template_config = {k: v for k, v in tpl.items()}
        ov = {} if not keep_existing else dict(b.overrides.get("config", {}))
        for k, tv in b.resolved_template_config.items():
            if asset.config.get(k) != tv:
                ov[k] = copy.deepcopy(asset.config.get(k))
            else:
                ov.pop(k, None)
        b.overrides["config"] = ov

    def before_config_update(self, asset, patch: dict, origin: str) -> None:
        if origin == "user" and self.category_external(asset.id, "configuration"):
            raise ConflictError(f"configuration of '{asset.id}' is driven by an external system "
                                "(sync category 'configuration' is external)")

    def after_config_update(self, asset, changed: set[str], origin: str) -> None:
        b = self.bindings.get(asset.id)
        if b is None or origin == "template":
            return
        for k in changed:
            if k in b.resolved_template_config:
                if asset.config.get(k) == b.resolved_template_config[k]:
                    b.overrides["config"].pop(k, None)
                else:
                    b.overrides["config"][k] = copy.deepcopy(asset.config.get(k))

    def _commit(self, asset, b: AssetBinding, *, reason: str | None, initial: bool = False,
                details: dict | None = None) -> None:
        """Validate a candidate binding and make it effective (raises without side effects)."""
        prof, problems = effective_profile(b)
        try:
            check_profile(prof, asset.kind)
        except ValidationError as exc:
            raise ValidationError(f"asset '{asset.id}': {exc}") from None
        typed = {p["name"] for p in b.template["profile"]["parameters"]}
        if b.overrides.get("parameters"):
            known = {k: v for k, v in b.overrides["parameters"].items() if any(p["name"] == k for p in prof["parameters"])}
            if known:
                b.overrides["parameters"] = {**b.overrides["parameters"],
                                             **validate_parameter_values(prof, known, typed_names=set())}
        old_b = self.bindings.get(asset.id)
        old_prof = self.runtime[asset.id].profile if asset.id in self.runtime else empty_profile()
        old_params = dict(self.runtime[asset.id].params) if asset.id in self.runtime else {}
        if b.type_id is None and profile_is_empty(b.local) and not any(b.overrides.get("parameters", {}).values()):
            self.bindings.pop(asset.id, None)
        else:
            self.bindings[asset.id] = b
        self._refresh(asset.id, prof, problems, b)
        if reason is None:
            return
        new_params = dict(self.runtime[asset.id].params) if asset.id in self.runtime else {}
        diff = diff_profiles(old_prof, prof)
        if not initial or diff or old_params != new_params or (old_b and old_b.type_version != b.type_version):
            asset.config_version += 1
            change_old = {"industrial.type": f"{old_b.type_id}@{old_b.type_version}" if old_b and old_b.type_id else None,
                          "industrial.parameters": old_params}
            change_new = {"industrial.type": f"{b.type_id}@{b.type_version}" if b.type_id else None,
                          "industrial.parameters": new_params}
            for s, d in diff.items():
                change_old[f"industrial.{s}"] = sorted(it[SECTION_KEY[s]] for it in old_prof[s])
                change_new[f"industrial.{s}"] = d
            boundary = ("production features: next cycle (active cycles keep their snapshot); "
                        "sensors and working conditions: immediate")
            change = self.sim._record_change(asset.id, change_old, change_new, asset.config_version, boundary)
            if change.changes:
                self.sim.emit("configuration_changed", asset, config={"industrial": reason},
                              details={"changes": change.changes, "config_version": asset.config_version,
                                       "effective": boundary, "reason": reason, **(details or {})})
            asset.record_conditions_change()
            self.sim.request_wake(asset.id)

    def _refresh(self, aid: str, prof: dict, problems: list, b: AssetBinding) -> None:
        if profile_is_empty(prof):
            rt = self.runtime.pop(aid, None)
            if rt:
                self._cancel_events(rt)
            return
        rt = self.runtime.get(aid)
        if rt is None:
            rt = self.runtime[aid] = AssetRuntime()
        rt.profile = copy.deepcopy(prof)
        rt.problems = list(problems)
        rt.params = parameter_values(prof, b)
        rt.order = composite_order(prof["composites"])
        sensors = {s["id"] for s in prof["sensors"]}
        for sid in [s for s in rt.sensors if s not in sensors]:
            del rt.sensors[sid]
        comps = {c["id"] for c in prof["composites"]}
        for cid in [c for c in rt.composite_values if c not in comps]:
            del rt.composite_values[cid]
        conds = {c["id"] for c in prof["conditions"]}
        for cid in [c for c in rt.conditions if c not in conds]:
            del rt.conditions[cid]
            self.sim.scheduler.cancel(rt.cond_events.pop(cid, None))
        self._reschedule(aid)
        if self.sim.started:
            self._request_eval(aid)

    def _cancel_events(self, rt: AssetRuntime) -> None:
        for seq in list(rt.sample_events.values()) + list(rt.cond_events.values()):
            self.sim.scheduler.cancel(seq)
        rt.sample_events.clear()
        rt.cond_events.clear()

    # ------------------------------------------------------------------ instance edits
    def set_parameters(self, asset_id: str, values: dict, *, origin: str = "user") -> dict:
        asset = self.sim.get_asset(asset_id)
        if origin == "user" and self.category_external(asset_id, "configuration"):
            raise ConflictError(f"configuration of '{asset_id}' is driven by an external system")
        rt = self.runtime.get(asset_id)
        if rt is None:
            raise ValidationError(f"asset '{asset_id}' has no parameters")
        b = copy.deepcopy(self.binding(asset_id))
        typed = {p["name"] for p in b.template["profile"]["parameters"]}
        vals = validate_parameter_values(rt.profile, values, typed_names=typed if origin == "user" else set())
        b.overrides["parameters"] = {**b.overrides.get("parameters", {}), **vals}
        self._commit(asset, b, reason=f"parameters changed ({origin})", details={"origin": origin})
        return self.describe(asset_id)

    def set_item_override(self, asset_id: str, section: str, key: str, patch: dict | None) -> dict:
        asset = self.sim.get_asset(asset_id)
        if section not in OVERRIDE_SECTIONS:
            raise ValidationError(f"overrides are supported for {list(OVERRIDE_SECTIONS)}")
        b = copy.deepcopy(self.binding(asset_id))
        if b.type_id is None:
            raise ConflictError(f"asset '{asset_id}' is not bound to an asset type; edit its local definitions")
        if not any(it[SECTION_KEY[section]] == key for it in b.template["profile"][section]):
            raise NotFoundError(f"template has no {section[:-1]} '{key}'")
        if patch:
            if not isinstance(patch, dict):
                raise ValidationError("override must be an object")
            b.overrides[section][key] = {**b.overrides[section].get(key, {}), **copy.deepcopy(patch)}
        else:
            b.overrides[section].pop(key, None)
        self._commit(asset, b, reason=f"override of {section[:-1]} '{key}' {'set' if patch else 'reset'}")
        return self.describe(asset_id)

    def reset_override(self, asset_id: str, section: str, key: str) -> dict:
        asset = self.sim.get_asset(asset_id)
        b = copy.deepcopy(self.binding(asset_id))
        if section == "config":
            if key not in b.overrides["config"]:
                raise NotFoundError(f"no configuration override for '{key}'")
            self.sim.update_config(asset_id, {key: copy.deepcopy(b.template["config"][key])}, origin="template")
            self.bindings[asset_id].overrides["config"].pop(key, None)
            return self.describe(asset_id)
        if section == "parameters":
            if key not in b.overrides["parameters"]:
                raise NotFoundError(f"no value override for parameter '{key}'")
            b.overrides["parameters"].pop(key)
            self._commit(asset, b, reason=f"parameter '{key}' reset to default")
            return self.describe(asset_id)
        return self.set_item_override(asset_id, section, key, None)

    def set_local(self, asset_id: str, local: dict) -> dict:
        asset = self.sim.get_asset(asset_id)
        b = copy.deepcopy(self.binding(asset_id))
        b.local = normalize_profile(local, "local")
        self._commit(asset, b, reason="instance-local definitions changed")
        return self.describe(asset_id)

    # ------------------------------------------------------------------ explicit template sync
    def sync_preview(self, asset_id: str) -> dict:
        asset = self.sim.get_asset(asset_id)
        b = self.binding(asset_id)
        if b.type_id is None:
            raise ConflictError(f"asset '{asset_id}' is not bound to an asset type")
        t = self.get_type(b.type_id)
        new_b, dropped = self._synced_binding(b, t)
        cfg_old, cfg_new = b.template["config"], t.config
        config_changes = {k: {"template_old": cfg_old.get(k), "template_new": cfg_new.get(k),
                              "instance_value": asset.config.get(k),
                              "result": asset.config.get(k) if k in b.overrides["config"] else cfg_new.get(k),
                              "overridden": k in b.overrides["config"]}
                          for k in sorted(set(cfg_old) | set(cfg_new)) if cfg_old.get(k) != cfg_new.get(k)}
        error = None
        try:
            prof, _ = effective_profile(new_b)
            check_profile(prof, asset.kind)
        except ValidationError as exc:
            error = str(exc)
        return {"asset_id": asset_id, "type_id": t.id, "from_version": b.type_version, "to_version": t.version,
                "outdated": (b.type_version or 0) < t.version,
                "template_changes": diff_profiles(b.template["profile"], t.profile),
                "config_changes": config_changes, "preserved_overrides": {
                    s: sorted(new_b.overrides[s]) for s in new_b.overrides if new_b.overrides[s]},
                "dropped_overrides": dropped, "valid": error is None, "error": error}

    def _synced_binding(self, b: AssetBinding, t: AssetType) -> tuple[AssetBinding, list[str]]:
        nb = copy.deepcopy(b)
        nb.type_version = t.version
        nb.template = t.template()
        dropped = []
        for s in OVERRIDE_SECTIONS:
            keys = {it[SECTION_KEY[s]] for it in t.profile[s]}
            for k in list(nb.overrides[s]):
                if k not in keys:
                    dropped.append(f"{s}.{k}")
                    nb.overrides[s].pop(k)
        pdefs = {p["name"]: p for p in t.profile["parameters"]} | {p["name"]: p for p in nb.local["parameters"]}
        for k in list(nb.overrides["parameters"]):
            d = pdefs.get(k)
            try:
                if d is None:
                    raise ValidationError("removed")
                nb.overrides["parameters"][k] = coerce_value(d, nb.overrides["parameters"][k], k)
            except ValidationError:
                dropped.append(f"parameters.{k}")
                nb.overrides["parameters"].pop(k)
        for k in list(nb.overrides["config"]):
            if k not in t.config:
                dropped.append(f"config.{k}")
                nb.overrides["config"].pop(k)
        return nb, dropped

    def sync_instance(self, asset_id: str, *, dry_run: bool = False) -> dict:
        preview = self.sync_preview(asset_id)
        if dry_run or not preview["valid"]:
            if not preview["valid"] and not dry_run:
                raise ValidationError(f"cannot synchronise '{asset_id}': {preview['error']}")
            return preview
        asset = self.sim.get_asset(asset_id)
        b = self.bindings[asset_id]
        t = self.get_type(b.type_id)
        nb, _ = self._synced_binding(b, t)
        target = {**copy.deepcopy(t.config), **copy.deepcopy(nb.overrides["config"])}
        patch = {k: v for k, v in target.items() if asset.config.get(k) != v}
        prof, _ = effective_profile(nb)
        check_profile(prof, asset.kind)
        if patch:
            self.sim.update_config(asset_id, patch, origin="template")
        self._track_config_overrides(asset, nb, keep_existing=True)
        nb.overrides["config"] = {k: v for k, v in nb.overrides["config"].items() if k in t.config}
        self._commit(asset, nb, reason=f"synchronised with asset type {t.id} v{t.version}",
                     details={"from_version": preview["from_version"], "to_version": t.version,
                              "dropped_overrides": preview["dropped_overrides"]})
        return {**preview, "applied": True}

    def sync_type_instances(self, type_id: str, *, dry_run: bool = False) -> list[dict]:
        self.get_type(type_id)
        return [self.sync_instance(a, dry_run=dry_run) for a, b in sorted(self.bindings.items())
                if b.type_id == type_id]

    # ------------------------------------------------------------------ synchronisation modes
    def sync_config(self, asset_id: str) -> dict:
        return self.sync.get(asset_id) or default_sync()

    def category_external(self, asset_id: str, category: str) -> bool:
        s = self.sync.get(asset_id)
        return bool(s) and s["categories"].get(category) == "external"

    def production_external(self, asset_id: str) -> bool:
        return self.category_external(asset_id, "production")

    def set_sync(self, asset_id: str, raw: dict, *, record: bool = True) -> dict:
        asset = self.sim.get_asset(asset_id)
        old = self.sync_config(asset_id)
        new = normalize_sync(raw, old)
        if new.get("external_id"):
            for aid, s in self.sync.items():
                if aid != asset_id and s.get("external_id") == new["external_id"]:
                    raise ConflictError(f"external id '{new['external_id']}' is already mapped to '{aid}'")
        if new["categories"]["production"] == "external" and asset.kind not in ("producer", "transformer"):
            raise ValidationError("external production lifecycle is supported for producers and transformers")
        if new == default_sync():
            self.sync.pop(asset_id, None)
        else:
            self.sync[asset_id] = new
        if record and old != new:
            self.sim._record_change(asset_id, {"sync": old}, {"sync": new}, asset.config_version,
                                    "immediate (synchronisation mode)")
            self.sim.emit("sync_mode_changed", asset, details={"old": old, "new": new})
        prod_changed = old["categories"]["production"] != new["categories"]["production"]
        if prod_changed and hasattr(asset, "on_sync_changed"):
            asset.on_sync_changed()
        if asset_id in self.runtime:
            self._reschedule(asset_id)
        if record and old != new:
            asset.record_conditions_change()
        self.sim.request_wake(asset_id)
        return new

    def find_asset_by_external_id(self, ext: str) -> str | None:
        for aid, s in self.sync.items():
            if s.get("external_id") == ext:
                return aid
        return ext if ext in self.sim.assets else None

    # ================================================================== lifecycle hooks
    def activate(self) -> None:
        for aid in list(self.runtime):
            self._reschedule(aid)
            self._request_eval(aid)

    def on_asset_removed(self, asset_id: str) -> None:
        rt = self.runtime.pop(asset_id, None)
        if rt:
            self._cancel_events(rt)
        self.bindings.pop(asset_id, None)
        self.sync.pop(asset_id, None)

    def active(self, asset_id: str) -> bool:
        return asset_id in self.runtime

    def sensor_source(self, asset_id: str, sensor: dict) -> str:
        if sensor["source"] == "external":
            return "external"
        if sensor["source"] == "simulated":
            return "simulated"
        return "external" if self.category_external(asset_id, "measurements") else "simulated"

    def _reschedule(self, aid: str) -> None:
        rt = self.runtime.get(aid)
        if rt is None:
            return
        if not self.sim.started:
            return
        wanted = {s["id"]: s for s in rt.profile["sensors"]
                  if s["enabled"] and s["model"]["type"] != "none" and self.sensor_source(aid, s) == "simulated"}
        for sid in [k for k in rt.sample_events if k not in wanted]:
            self.sim.scheduler.cancel(rt.sample_events.pop(sid))
        for sid, s in wanted.items():
            if sid not in rt.sample_events:
                rt.sample_events[sid] = self.sim.scheduler.schedule(self.sim.now, SAMPLE_PRIORITY, "ind:sample", aid, sid)

    def _request_eval(self, aid: str) -> None:
        rt = self.runtime.get(aid)
        if rt is None or rt.eval_pending or not self.sim.started or not (rt.profile["conditions"]
                                                                         or rt.profile["composites"]):
            return
        rt.eval_pending = True
        self.sim.scheduler.schedule(self.sim.now, SAMPLE_PRIORITY, "ind:eval", aid, None)

    def handle_event(self, ev) -> None:
        rt = self.runtime.get(ev.target)
        if rt is None:
            return
        if ev.kind == "ind:sample":
            sid = ev.payload
            if rt.sample_events.get(sid) != ev.seq:
                return
            rt.sample_events.pop(sid, None)
            s = rt.defs("sensors").get(sid)
            if s is None or not s["enabled"] or self.sensor_source(ev.target, s) != "simulated":
                return
            rt.sample_events[sid] = self.sim.scheduler.schedule(self.sim.now + s["sample_interval_s"], SAMPLE_PRIORITY,
                                                                "ind:sample", ev.target, sid)
            self.sample_sensor(ev.target, s)
        elif ev.kind == "ind:cond":
            if rt.cond_events.get(ev.payload) == ev.seq:
                rt.cond_events.pop(ev.payload, None)
                self.evaluate_conditions(ev.target)
        elif ev.kind == "ind:eval":
            rt.eval_pending = False
            self._propagate(ev.target, set(), evaluate_conditions=True)

    # ================================================================== variable resolution
    def _active_cycle(self, asset):
        cid = getattr(asset, "active_cycle_id", None)
        return self.sim.cycles.get(cid) if cid else None

    def nominal_features(self, asset) -> dict:
        cyc = self._active_cycle(asset)
        if cyc is not None:
            snap = cyc.config_snapshot
            return {f: snap.get(f) for f in FEATURES.get(asset.kind, ())}
        if asset.kind == "transformer":
            from ..curves import defect_probability
            speed = asset.config["speed_pct"]
            mult = asset.time_multiplier(speed)
            q = asset.quality_score(speed)
            r = asset.recipe
            return {"speed_pct": speed, "time_multiplier": mult,
                    "processing_time_s": r.processing_time_s * mult if r else None,
                    "quality_score": q, "defect_probability": defect_probability(q)}
        if asset.kind == "producer":
            return {"interval_s": asset.interval_s, "batch_quantity": asset.config["batch_quantity"],
                    "defect_probability": asset.config["defect_probability"]}
        return {}

    def _resolve(self, aid: str, rt: AssetRuntime, name: str, features: dict | None) -> Any:
        sim = self.sim
        if name == "t":
            return sim.now
        head, _, rest = name.partition(".")
        if head == "param":
            return rt.params.get(rest)
        if head == "sensor":
            return self.store.latest_value(aid, rest)
        if head == "composite":
            return rt.composite_values.get(rest)
        asset = sim.assets.get(aid)
        if asset is None:
            return None
        if head == "feature":
            if features is not None and rest in features:
                return features[rest]
            return self.nominal_features(asset).get(rest)
        if head == "condition":
            st = rt.conditions.get(rest)
            return bool(st and st["active"])
        if head == "state":
            if rest == "operational":
                return asset.is_operational()
            return asset.state == rest
        if head == "cycle":
            cyc = self._active_cycle(asset)
            if rest == "active":
                return cyc is not None
            if rest == "elapsed_s":
                return (sim.now - cyc.started_at) if cyc else 0.0
            if rest == "progress":
                if cyc is None:
                    return 0.0
                total = (cyc.scheduled_end or cyc.started_at) - cyc.started_at
                return 1.0 if total <= 0 else max(0.0, min(1.0, (sim.now - cyc.started_at) / total))
            return None
        if head == "recipe":
            recipe = getattr(asset, "recipe", None)
            if recipe is None:
                return None
            if rest == "processing_time_s":
                return recipe.processing_time_s
            v = recipe.parameters.get(rest)
            return v if isinstance(v, (int, float, str, bool)) else None
        if head == "config":
            v = asset.config.get(rest)
            return v if isinstance(v, (int, float, str, bool)) else None
        return None

    def _eval(self, aid: str, rt: AssetRuntime, source: str, env: _Env, what: str) -> Any:
        """Evaluate; returns None when inputs are missing; records evaluation errors."""
        try:
            return compile_expression(source).evaluate(env)
        except MissingValue:
            return None
        except EvaluationError as exc:
            self.stats["evaluation_errors"] += 1
            rt.errors.append({"time": self.sim.now, "what": what, "error": str(exc)})
            return None

    # ================================================================== sensing
    def sample_sensor(self, aid: str, s: dict, *, cycle_id: str | None = None, propagate: bool = True) -> Observation | None:
        rt = self.runtime[aid]
        srt: SensorRuntime = rt.sensors.setdefault(s["id"], SensorRuntime())
        now = self.sim.now
        dt = 0.0 if srt.last_sample is None else max(0.0, now - srt.last_sample)
        env = _Env(self, aid, rt, extra={"dt": dt, "self": srt.value})
        m = s["model"]
        x: float | None
        if m["type"] == "constant":
            x = m["value"]
        elif m["type"] == "expression":
            v = self._eval(aid, rt, m["expression"], env, f"sensor {s['id']}")
            x = None if v is None else _finite(v)
        elif m["type"] == "first_order":
            target = self._eval(aid, rt, m["target"], env, f"sensor {s['id']} target")
            target = None if target is None else _finite(target)
            if target is None:
                x = None
            else:
                x0 = srt.value if srt.value is not None else m["initial"]
                x = target + (x0 - target) * math.exp(-dt / m["time_constant_s"])
        elif m["type"] == "random_walk":
            x0 = srt.value if srt.value is not None else m["initial"]
            x = x0 + (self.rng.gauss(0.0, m["step_std"] * math.sqrt(dt)) if dt > 0 and m["step_std"] > 0 else 0.0)
        else:
            return None
        if x is None:
            return None
        srt.value = x
        srt.last_sample = now
        raw = s["gain"] * x + s["offset"]
        if s["noise_std"] > 0:
            raw += self.rng.gauss(0.0, s["noise_std"])
        value = self._filter(s, srt, raw)
        self.stats["samples"] += 1
        return self.record_observation(aid, s, value, raw_value=raw, source="simulated", cycle_id=cycle_id,
                                       propagate=propagate)

    @staticmethod
    def _filter(s: dict, srt: SensorRuntime, raw: float) -> float:
        f = s["filter"]
        if f["type"] == "moving_average":
            srt.window.append(raw)
            while len(srt.window) > f["window"]:
                srt.window.popleft()
            return sum(srt.window) / len(srt.window)
        if f["type"] == "ema":
            srt.ema = raw if srt.ema is None else f["alpha"] * raw + (1 - f["alpha"]) * srt.ema
            return srt.ema
        return raw

    @staticmethod
    def _quantize(s: dict, value: Any) -> Any:
        if isinstance(value, bool):
            return value if s["data_type"] == "boolean" else float(value)
        if s["data_type"] == "boolean":
            return bool(value)
        if s["resolution"]:
            value = round(round(value / s["resolution"]) * s["resolution"], 10)
        if s["data_type"] == "integer":
            return int(round(value))
        return float(value)

    def record_observation(self, aid: str, s: dict, value: Any, *, raw_value: Any = None, source: str,
                           quality: str | None = None, cycle_id: str | None = None, sim_time: float | None = None,
                           source_timestamp: str | None = None, message_id: str | None = None,
                           product_id: str | None = None, propagate: bool = True,
                           update_latest: bool = True) -> Observation:
        value = self._quantize(s, value)
        if quality is None:
            quality = "good"
        if quality in ("good", "late", "estimated") and isinstance(value, (int, float)) and not isinstance(value, bool):
            if (s["min"] is not None and value < s["min"]) or (s["max"] is not None and value > s["max"]):
                quality = "out_of_range"
        asset = self.sim.assets.get(aid)
        if cycle_id is None and asset is not None:
            cyc = self._active_cycle(asset)
            cycle_id = cyc.id if cyc else None
        obs = Observation(seq=self.store.next_seq(), asset_id=aid, measurement_id=s["id"], kind="sensor",
                          value=value, unit=s["unit"], sim_time=self.sim.now if sim_time is None else sim_time,
                          source=source, quality=quality, raw_value=raw_value, cycle_id=cycle_id,
                          source_timestamp=source_timestamp, received_at=self.sim.now, message_id=message_id,
                          product_id=product_id)
        self.store.add(obs, update_latest=update_latest)
        self._observed(obs)
        if propagate and update_latest and quality in ("good", "late", "estimated"):
            self._propagate(aid, {f"sensor.{s['id']}"}, evaluate_conditions=True)
        return obs

    def _observed(self, obs: Observation) -> None:
        ds = getattr(self.sim, "datasets", None)
        if ds is not None:
            ds.on_observation(obs)

    def _propagate(self, aid: str, dirty: set[str], *, evaluate_conditions: bool) -> None:
        rt = self.runtime.get(aid)
        if rt is None:
            return
        if rt.order:
            comps = rt.defs("composites")
            env = _Env(self, aid, rt)
            asset = self.sim.assets.get(aid)
            cyc = self._active_cycle(asset) if asset else None
            for cid in rt.order:
                c = comps[cid]
                variables = compile_expression(c["expression"]).variables
                measured_only = all(v.startswith(("sensor.", "composite.")) for v in variables)
                if measured_only and not (variables & dirty) and cid in rt.composite_values:
                    continue
                v = self._eval(aid, rt, c["expression"], env, f"composite {cid}")
                quality = "good"
                if v is not None:
                    try:
                        v = _finite(v) if not isinstance(v, bool) else v
                    except EvaluationError:
                        v = None
                if v is None:
                    if c["missing"] == "default" and c["default"] is not None:
                        v, quality = c["default"], "estimated"
                    elif c["missing"] == "last":
                        continue
                prev = rt.composite_values.get(cid)
                rt.composite_values[cid] = v
                if v is None:
                    if prev is not None:
                        dirty.add(f"composite.{cid}")
                    continue
                dirty.add(f"composite.{cid}")
                self.stats["composites"] += 1
                obs = Observation(seq=self.store.next_seq(), asset_id=aid, measurement_id=cid, kind="composite",
                                  value=v, unit=c["unit"], sim_time=self.sim.now, source="composite",
                                  quality=quality, cycle_id=cyc.id if cyc else None, received_at=self.sim.now)
                self.store.add(obs)
                self._observed(obs)
        if evaluate_conditions:
            self.evaluate_conditions(aid)

    # ================================================================== working conditions
    def evaluate_conditions(self, aid: str) -> None:
        rt = self.runtime.get(aid)
        asset = self.sim.assets.get(aid)
        if rt is None or asset is None or not rt.profile["conditions"]:
            return
        now = self.sim.now
        env = _Env(self, aid, rt)
        for c in sorted(rt.profile["conditions"], key=lambda c: (c["priority"], c["id"])):
            st = rt.conditions.setdefault(c["id"], {"active": False, "pending_since": None, "since": None,
                                                    "activations": 0, "status": "unknown"})
            if not c["enabled"]:
                if st["active"]:
                    self._transition(asset, rt, c, st, False, env, reason="disabled")
                continue
            if not st["active"]:
                res = self._eval(aid, rt, c["activation"], env, f"condition {c['id']}")
                if res is None:
                    st["status"] = "missing_inputs"
                    continue
                st["status"] = "evaluated"
                want = bool(res)
                delay = c["delay_on_s"]
            else:
                if c["deactivation"]:
                    res = self._eval(aid, rt, c["deactivation"], env, f"condition {c['id']} deactivation")
                    want = None if res is None else not bool(res)
                else:
                    res = self._eval(aid, rt, c["activation"], env, f"condition {c['id']}")
                    want = None if res is None else bool(res)
                if want is None:
                    st["status"] = "missing_inputs"
                    continue
                st["status"] = "evaluated"
                delay = c["delay_off_s"]
            if want == st["active"]:
                st["pending_since"] = None
                self.sim.scheduler.cancel(rt.cond_events.pop(c["id"], None))
                continue
            if st["pending_since"] is None:
                st["pending_since"] = now
            if now - st["pending_since"] >= delay - 1e-9:
                self._transition(asset, rt, c, st, want, env)
            elif c["id"] not in rt.cond_events:
                rt.cond_events[c["id"]] = self.sim.scheduler.schedule(st["pending_since"] + delay, SAMPLE_PRIORITY,
                                                                      "ind:cond", aid, c["id"])

    def _transition(self, asset, rt: AssetRuntime, c: dict, st: dict, on: bool, env: _Env,
                    reason: str | None = None) -> None:
        now = self.sim.now
        st.update(active=on, since=now, pending_since=None)
        self.sim.scheduler.cancel(rt.cond_events.pop(c["id"], None))
        if on:
            st["activations"] += 1
        variables = set(compile_expression(c["activation"]).variables)
        if c["deactivation"]:
            variables |= compile_expression(c["deactivation"]).variables
        readings = {v: _jsonable(env.get(v)) for v in sorted(variables)}
        classification = self.classification(asset.id)
        rec = {"time": now, "asset_id": asset.id, "condition_id": c["id"], "name": c["name"],
               "severity": c["severity"], "active": on, "measurements": readings, "action": c["action"],
               "effects": copy.deepcopy(c["effects"]), "classification": classification, "reason": reason}
        self.transitions.append(rec)
        self.stats["transitions"] += 1
        self.sim.emit("condition_activated" if on else "condition_cleared", asset, status=asset.state,
                      readings=readings, error=(c["message"] or None) if on else None,
                      details={"condition_id": c["id"], "name": c["name"], "severity": c["severity"],
                               "priority": c["priority"], "action": c["action"], "effects": c["effects"],
                               "classification": classification, "reason": reason})
        asset.record_conditions_change()
        tag = f"condition:{c['id']}"
        if on and c["action"] in ("stop", "fault") and asset.is_operational():
            asset.on_stop(f"working condition '{c['id']}' ({c['severity']})")
            asset.set_state("stopped" if c["action"] == "stop" else "failed", tag)
        elif not on and c["action"] == "stop" and asset.state == "stopped" and asset.state_reason == tag:
            asset.command("start")
        if c["action"] == "block":
            self.sim.request_wake(asset.id)
        ds = getattr(self.sim, "datasets", None)
        if ds is not None:
            ds.on_condition(asset.id, c["id"], on)

    def active_conditions(self, aid: str) -> list[dict]:
        rt = self.runtime.get(aid)
        if rt is None:
            return []
        return [c for c in sorted(rt.profile["conditions"], key=lambda c: (c["priority"], c["id"]))
                if rt.conditions.get(c["id"], {}).get("active")]

    def classification(self, aid: str) -> str:
        act = self.active_conditions(aid)
        if not act:
            return "normal"
        return max(act, key=lambda c: (SEVERITY_RANK[c["severity"]], c["priority"]))["severity"]

    def blocking_condition(self, aid: str) -> str | None:
        for c in self.active_conditions(aid):
            if c["action"] == "block":
                return c["id"]
        return None

    def interval_conditions(self, aid: str) -> dict:
        rt = self.runtime.get(aid)
        if rt is None or not rt.profile["conditions"]:
            return {}
        return {"working_conditions": [c["id"] for c in self.active_conditions(aid)],
                "classification": self.classification(aid)}

    # ================================================================== behaviour models & features
    def cycle_features(self, asset, recipe=None) -> dict | None:
        """Production features for a cycle/batch starting now (None when the asset has no profile)."""
        rt = self.runtime.get(asset.id)
        if rt is None:
            return None
        if asset.kind not in FEATURES:
            return None
        feats: dict[str, Any] = {}
        applied: list[dict] = []
        env = _Env(self, asset.id, rt, features=feats)
        behaviors = [b for b in rt.profile["behaviors"] if b["enabled"]]
        effects = [(c, e) for c in self.active_conditions(asset.id) for e in c["effects"]]

        def run(f: str, value: float) -> float:
            feats[f] = value
            for b in behaviors:
                if b["output"] != f:
                    continue
                env.extra = {"self": value}
                if b["when"]:
                    w = self._eval(asset.id, rt, b["when"], env, f"behavior {b['id']} when")
                    if not w:
                        continue
                bad = []
                for var, (lo, hi) in b["input_ranges"].items():
                    iv = env.get(var)
                    if not isinstance(iv, (int, float)) or (lo is not None and iv < lo) or (hi is not None and iv > hi):
                        bad.append(var)
                if bad:
                    applied.append({"behavior": b["id"], "version": b["version"], "output": f, "skipped": True,
                                    "reason": f"input(s) outside valid range: {', '.join(bad)}"})
                    continue
                v = self._eval(asset.id, rt, b["expression"], env, f"behavior {b['id']}")
                if v is None or isinstance(v, str):
                    applied.append({"behavior": b["id"], "version": b["version"], "output": f, "skipped": True,
                                    "reason": "missing or invalid inputs"})
                    continue
                try:
                    v = _finite(v)
                except EvaluationError:
                    continue
                if b["min"] is not None:
                    v = max(b["min"], v)
                if b["max"] is not None:
                    v = min(b["max"], v)
                applied.append({"behavior": b["id"], "version": b["version"], "output": f, "old": value, "new": v})
                self.stats["behaviors_applied"] += 1
                value = feats[f] = v
            for c, e in effects:
                if e["target"] != f:
                    continue
                env.extra = {"self": value}
                ev = e["value"]
                if isinstance(ev, str):
                    ev = self._eval(asset.id, rt, ev, env, f"condition {c['id']} effect")
                    if ev is None or isinstance(ev, str):
                        continue
                    ev = _finite(ev)
                old = value
                value = {"multiply": lambda a, b: a * b, "add": lambda a, b: a + b, "set": lambda a, b: b,
                         "min": min, "max": max}[e["op"]](value, ev)
                applied.append({"condition": c["id"], "severity": c["severity"], "output": f, "op": e["op"],
                                "operand": ev, "old": old, "new": value})
                feats[f] = value
            lo, hi = FEATURE_BOUNDS.get(f, (None, None))
            if lo is not None:
                value = max(lo, value)
            if hi is not None:
                value = min(hi, value)
            feats[f] = value
            return value

        if asset.kind == "transformer":
            from ..curves import defect_probability
            speed = run("speed_pct", asset.config["speed_pct"])
            mult = run("time_multiplier", asset.time_multiplier(speed))
            run("processing_time_s", (recipe.processing_time_s if recipe else 0.0) * mult)
            q = run("quality_score", asset.quality_score(speed))
            run("defect_probability", defect_probability(q))
        else:
            run("interval_s", asset.interval_s)
            feats["batch_quantity"] = int(round(run("batch_quantity", float(asset.config["batch_quantity"]))))
            run("defect_probability", asset.config["defect_probability"])
        b = self.bindings.get(asset.id)
        out = {"features": dict(feats), "applied": applied, "parameters": copy.deepcopy(rt.params),
               "working_conditions": [c["id"] for c in self.active_conditions(asset.id)],
               "classification": self.classification(asset.id),
               "asset_type": {"id": b.type_id, "version": b.type_version} if b and b.type_id else None}
        rt.last_features = out
        return out

    def on_cycle_started(self, asset, cycle) -> None:
        """Tag cycle-start observations (simulated sensors sampled at the boundary)."""
        rt = self.runtime.get(asset.id)
        if rt is None:
            return
        self._boundary_sample(asset.id, rt, cycle.id)

    def on_cycle_ending(self, asset, cycle) -> None:
        rt = self.runtime.get(asset.id)
        if rt is not None:
            self._boundary_sample(asset.id, rt, cycle.id)
        cycle.measurements = self.store.cycle_summary(cycle.id)

    def _boundary_sample(self, aid: str, rt: AssetRuntime, cycle_id: str) -> None:
        sampled = False
        for s in rt.profile["sensors"]:
            if s["enabled"] and s["model"]["type"] != "none" and self.sensor_source(aid, s) == "simulated":
                if self.sample_sensor(aid, s, cycle_id=cycle_id, propagate=False) is not None:
                    sampled = True
        if sampled:
            self._propagate(aid, {f"sensor.{s['id']}" for s in rt.profile["sensors"]}, evaluate_conditions=False)
            self._request_eval(aid)

    # ================================================================== attribute mappings
    def _input_attr(self, product, name: str) -> Any:
        if name in product.attributes:
            return product.attributes[name]
        if name in ("quality_score", "is_defective", "lot_number", "product_type_id", "label", "id",
                    "production_timestamp", "external_id"):
            return getattr(product, name, None)
        if name.startswith("measurements."):
            return product.measurements.get(name.split(".", 1)[1])
        return None

    def _cycle_measure(self, aid: str, cycle, mid: str, agg: str) -> tuple[Any, dict]:
        aggs = self.store.cycle_aggregates.get(cycle.id, {}) if cycle is not None else {}
        a = aggs.get(mid)
        if a is not None and a.count:
            try:
                return a.get(agg), {"cycle_id": cycle.id, "observation_count": a.count, "first_time": a.first_time,
                                    "last_time": a.last_time, "unit": a.unit, "basis": "cycle"}
            except KeyError:
                return None, {}
        if agg in ("final", "latest", "initial"):
            obs = self.store.latest.get((aid, mid))
            if obs is not None:
                return obs.value, {"observation_seq": obs.seq, "timestamp": obs.sim_time, "unit": obs.unit,
                                   "source_timestamp": obs.source_timestamp, "basis": "latest"}
        return None, {}

    def _cycle_field(self, ctx: dict, name: str) -> Any:
        cyc = ctx.get("cycle")
        if name == "output_slot":
            return ctx.get("slot")
        if name == "unit_index":
            return ctx.get("unit_index")
        if cyc is None:
            return None
        if name == "cycle_id":
            return cyc.id
        if name == "duration_s":
            return (cyc.ended_at if cyc.ended_at is not None else self.sim.now) - cyc.started_at
        if name == "ended_at":
            return cyc.ended_at if cyc.ended_at is not None else self.sim.now
        if name == "asset_name":
            a = self.sim.assets.get(cyc.asset_id)
            return a.name if a else None
        return getattr(cyc, name, None)

    def resolve_attributes(self, asset, *, product_type_id: str, mappings: list[dict], cycle,
                           inputs: list | None = None, input_slots: dict | None = None,
                           features: dict | None = None, static: dict | None = None,
                           slot: int | None = None, is_byproduct: bool = False, unit_index: int = 0,
                           external: dict | None = None) -> tuple[dict, dict]:
        """Evaluate attribute mappings for one output product. Returns (attributes, provenance)."""
        sim = self.sim
        pt = sim.product_types.get(product_type_id)
        defs = {a["name"]: a for a in (getattr(pt, "attributes", None) or [])}
        attrs: dict[str, Any] = {}
        prov: dict[str, dict] = {}
        base = {"asset_id": asset.id, "cycle_id": cycle.id if cycle else None, "evaluated_at": sim.now}
        for k, v in (static or {}).items():
            attrs[k] = copy.deepcopy(v)
            prov[k] = {**base, "kind": "constant", "source_type": "static_config", "status": "ok"}
        rt = self.runtime.get(asset.id) or AssetRuntime()
        ctx = {"attrs": attrs, "inputs": inputs or [], "input_slots": input_slots or {}, "cycle": cycle,
               "slot": slot, "is_byproduct": is_byproduct,
               "product_type_id": product_type_id, "unit_index": unit_index}
        env = _MappingEnv(self, asset.id, rt, ctx, features)
        for m in mappings:
            name = m["attribute"]
            p: dict[str, Any] = {**base, "source_type": m["source"], "mapping": _mapping_desc(m)}
            if m.get("when"):
                w = self._eval(asset.id, rt, m["when"], env, f"mapping {name} when")
                if not w:
                    p.update(kind="skipped", status="condition_false")
                    prov.setdefault(name, p)
                    continue
            value, info, kind = self._mapping_value(asset, rt, m, env, ctx, features)
            p.update(info)
            p["kind"] = kind
            if value is None:
                pol = m["on_missing"]
                if pol == "skip":
                    p["status"] = "missing_skipped"
                    prov[name] = p
                    continue
                if pol == "default":
                    if m["default"] is not None:
                        value, p["status"] = m["default"], "missing_default"
                    elif name in defs and defs[name]["default"] is not None:
                        value, p["status"] = copy.deepcopy(defs[name]["default"]), "missing_type_default"
                    else:
                        p["status"] = "missing"
                        prov[name] = p
                        continue
                else:
                    p["status"] = "missing_null"
            else:
                if isinstance(value, (int, float)) and not isinstance(value, bool) and (m["scale"] != 1.0 or m["offset"]):
                    value = value * m["scale"] + m["offset"]
                    p["transform"] = {"scale": m["scale"], "offset": m["offset"]}
                p["status"] = "ok"
            if name in defs and value is not None:
                try:
                    value = coerce_value(defs[name], value, f"attribute '{name}'")
                except ValidationError as exc:
                    self.stats["mapping_errors"] += 1
                    p["status"] = "invalid"
                    p["error"] = str(exc)
                    p["rejected_value"] = _jsonable(value)
                    value = copy.deepcopy(defs[name]["default"])
                    if value is None:
                        prov[name] = p
                        continue
            attrs[name] = value
            prov[name] = p
        for k, v in (external or {}).items():
            if k in defs:
                try:
                    v = coerce_value(defs[k], v, f"attribute '{k}'")
                except ValidationError as exc:
                    self.stats["mapping_errors"] += 1
                    prov[k] = {**base, "kind": "external", "source_type": "external", "status": "invalid",
                               "error": str(exc), "rejected_value": _jsonable(v)}
                    continue
            attrs[k] = v
            prov[k] = {**base, "kind": "external", "source_type": "external", "status": "ok"}
        for name, d in defs.items():
            if name not in attrs and d["default"] is not None:
                attrs[name] = copy.deepcopy(d["default"])
                prov[name] = {**base, "kind": "default", "source_type": "attribute_default",
                              "status": prov.get(name, {}).get("status", "ok") if name in prov else "ok"}
            if d["required"] and name not in attrs:
                self.stats["mapping_errors"] += 1
                prov.setdefault(name, {**base, "kind": "missing", "source_type": None})
                prov[name]["status"] = "required_missing"
        return attrs, prov

    def _mapping_value(self, asset, rt: AssetRuntime, m: dict, env: _MappingEnv, ctx: dict,
                       features: dict | None) -> tuple[Any, dict, str]:
        src = m["source"]
        if src == "constant":
            return copy.deepcopy(m["value"]), {}, "constant"
        if src == "parameter":
            ref = m["ref"]
            if ref in rt.params:
                return rt.params[ref], {"ref": ref, "parameter": ref}, "copied"
            snap = ctx["cycle"].config_snapshot if ctx.get("cycle") else {}
            if ref in snap and isinstance(snap[ref], (int, float, str, bool)):
                return snap[ref], {"ref": ref, "config_key": ref, "basis": "cycle_snapshot"}, "copied"
            v = asset.config.get(ref)
            return (v if isinstance(v, (int, float, str, bool)) else None), {"ref": ref, "config_key": ref}, "copied"
        if src == "measurement":
            v, info = self._cycle_measure(asset.id, ctx.get("cycle"), m["ref"], m["aggregation"])
            kind = "copied" if m["aggregation"] in ("final", "latest", "initial") else "aggregated"
            return v, {"ref": m["ref"], "sensor": m["ref"], "aggregation": m["aggregation"], **info}, kind
        if src == "feature":
            f = (features or {}).get(m["ref"])
            if f is None and ctx.get("cycle") is not None:
                f = ctx["cycle"].config_snapshot.get(m["ref"])
            return f, {"ref": m["ref"], "feature": m["ref"]}, "copied"
        if src == "condition":
            if m["ref"] == "classification":
                return self.classification(asset.id), {"ref": "classification"}, "copied"
            if m["ref"] not in rt.conditions and m["ref"] not in {c["id"] for c in rt.profile["conditions"]}:
                return None, {"ref": m["ref"]}, "copied"
            return bool(rt.conditions.get(m["ref"], {}).get("active")), {"ref": m["ref"], "condition": m["ref"]}, "copied"
        if src == "order":
            cyc = ctx.get("cycle")
            oid = cyc.production_order_ids[0] if cyc and cyc.production_order_ids else None
            o = self.sim.orders.get(oid) if oid else None
            if o is None:
                return None, {"ref": m["ref"]}, "metadata"
            v = {"id": o.id, "name": o.name}.get(m["ref"], o.attributes.get(m["ref"]))
            return _jsonable(v), {"ref": m["ref"], "order_id": o.id}, "metadata"
        if src == "cycle":
            return _jsonable(self._cycle_field(ctx, m["ref"])), {"ref": m["ref"]}, "metadata"
        if src == "expression":
            v = self._eval(asset.id, rt, m["expression"], env, f"mapping {m['attribute']}")
            return v, {"formula": m["expression"]}, "calculated"
        if src == "input":
            prods = list(ctx.get("inputs") or [])
            slots = ctx.get("input_slots") or {}
            if m.get("input_slot") is not None:
                prods = [p for p in prods if slots.get(p.id) == m["input_slot"]]
            if m.get("input_product_type_id"):
                prods = [p for p in prods if p.product_type_id == m["input_product_type_id"]]
            pairs = [(p, self._input_attr(p, m["ref"])) for p in prods]
            present = [(p, v) for p, v in pairs if v is not None]
            info = {"ref": m["ref"], "aggregation": m["aggregation"], "input_product_ids": [p.id for p, _ in present]}
            return _aggregate_inputs(m, present, self), info, "inherited"
        return None, {}, "unknown"

    # ================================================================== views
    def snapshot_summary(self) -> dict:
        out = {}
        for aid in set(self.runtime) | set(self.bindings) | set(self.sync):
            if aid not in self.sim.assets:
                continue
            b = self.bindings.get(aid)
            t = self.asset_types.get(b.type_id) if b and b.type_id else None
            out[aid] = {"classification": self.classification(aid),
                        "active_conditions": [{"id": c["id"], "name": c["name"], "severity": c["severity"]}
                                              for c in self.active_conditions(aid)],
                        "type_id": b.type_id if b else None, "type_version": b.type_version if b else None,
                        "outdated": bool(t and (b.type_version or 0) < t.version),
                        "sync_mode": self.sync_config(aid)["mode"]}
        return out

    def describe(self, asset_id: str) -> dict:
        asset = self.sim.get_asset(asset_id)
        b = self.binding(asset_id)
        t = self.asset_types.get(b.type_id) if b.type_id else None
        rt = self.runtime.get(asset_id)
        prof, problems = effective_profile(b)
        tpl_keys = {s: {it[SECTION_KEY[s]] for it in b.template["profile"][s]} for s in SECTIONS}
        sections = {}
        for s in SECTIONS:
            items = []
            for it in prof[s]:
                k = it[SECTION_KEY[s]]
                origin = "local" if k not in tpl_keys[s] else (
                    "override" if k in b.overrides.get(s, {}) else "template")
                items.append({**copy.deepcopy(it), "origin": origin})
            sections[s] = items
        params = []
        values = rt.params if rt else parameter_values(prof, b)
        for p in prof["parameters"]:
            params.append({**copy.deepcopy(p), "value": values.get(p["name"]),
                           "overridden": p["name"] in b.overrides.get("parameters", {}),
                           "origin": "template" if p["name"] in tpl_keys["parameters"] else "local"})
        sections["parameters"] = params
        latest = {mid: o.to_dict() for mid, o in self.store.latest_for(asset_id).items()}
        sensors = []
        for s in sections["sensors"]:
            s["effective_source"] = self.sensor_source(asset_id, s)
            s["latest"] = latest.get(s["id"])
            sensors.append(s)
        for c in sections["composites"]:
            c["value"] = rt.composite_values.get(c["id"]) if rt else None
            c["latest"] = latest.get(c["id"])
        for c in sections["conditions"]:
            st = (rt.conditions.get(c["id"]) if rt else None) or {}
            c["state"] = {"active": bool(st.get("active")), "since": st.get("since"),
                          "pending_since": st.get("pending_since"), "activations": st.get("activations", 0),
                          "status": st.get("status", "unknown")}
        cfg = []
        for k, v in sorted(asset.config.items()):
            in_tpl = k in b.template["config"]
            cfg.append({"key": k, "value": copy.deepcopy(v),
                        "template_value": copy.deepcopy(b.resolved_template_config.get(k, b.template["config"].get(k)))
                        if in_tpl else None,
                        "origin": ("override" if k in b.overrides.get("config", {}) else "template") if in_tpl
                        else "instance"})
        return {
            "asset_id": asset_id, "kind": asset.kind,
            "type": None if b.type_id is None else {
                "id": b.type_id, "name": t.name if t else None, "bound_version": b.type_version,
                "current_version": t.version if t else None,
                "outdated": bool(t and (b.type_version or 0) < t.version)},
            "config": cfg, **sections,
            "overrides": copy.deepcopy(b.overrides), "local": copy.deepcopy(b.local),
            "problems": problems + (list(rt.problems) if rt else []),
            "classification": self.classification(asset_id),
            "active_conditions": [c["id"] for c in self.active_conditions(asset_id)],
            "last_features": copy.deepcopy(rt.last_features) if rt else None,
            "evaluation_errors": list(rt.errors) if rt else [],
            "sync": copy.deepcopy(self.sync_config(asset_id)),
            "features": list(FEATURES.get(asset.kind, ())),
        }

    def condition_log(self, *, asset_id: str | None = None, limit: int = 200) -> list[dict]:
        out = [r for r in self.transitions if asset_id is None or r["asset_id"] == asset_id]
        return copy.deepcopy(out[-limit:])

    def check_expression(self, expression: Any, *, asset_id: str | None = None, type_id: str | None = None,
                         kind: str | None = None) -> dict:
        """Validate an expression against an asset/type context and, for a live asset, preview its value."""
        from .definitions import variable_checker
        from .expressions import validate_expression
        rt = None
        if asset_id is not None:
            asset = self.sim.get_asset(asset_id)
            rt = self.runtime.get(asset_id)
            profile, kind = (rt.profile if rt else empty_profile()), asset.kind
        elif type_id is not None:
            t = self.get_type(type_id)
            profile, kind = t.profile, t.kind
        else:
            profile = empty_profile()
        allowed = variable_checker(profile, kind) if kind else None
        try:
            src = validate_expression(expression, "expression", allowed)
        except ValidationError as exc:
            return {"valid": False, "error": str(exc)}
        out: dict[str, Any] = {"valid": True, "expression": src,
                               "variables": sorted(compile_expression(src).variables)}
        if asset_id is not None and rt is not None:
            env = _Env(self, asset_id, rt, extra={"self": None})
            try:
                out["value"] = _jsonable(compile_expression(src).evaluate(env))
                out["status"] = "ok"
            except MissingValue as exc:
                out["value"], out["status"] = None, f"missing input: {exc}"
            except EvaluationError as exc:
                out["value"], out["status"] = None, f"error: {exc}"
        return out

    def to_scenario(self) -> dict:
        return {"asset_types": [t.to_dict() for t in self.asset_types.values()],
                "bindings": {a: b.to_dict() for a, b in self.bindings.items() if a in self.sim.assets},
                "sync": copy.deepcopy({a: s for a, s in self.sync.items() if a in self.sim.assets})}

    def load_types(self, types: list[dict]) -> None:
        for d in types or []:
            t = AssetType.from_dict(d)
            if t.id in self.asset_types:
                raise ConflictError(f"asset type '{t.id}' already exists")
            self._validate_type(t)
            self.asset_types[t.id] = t

    # ================================================================== default asset types
    @staticmethod
    def default_type_id(kind: str) -> str:
        return f"{DEFAULT_TYPE_PREFIX}{kind}"

    def ensure_default_types(self) -> list[str]:
        """Create the per-kind default asset types (empty template) when missing; returns the created ids."""
        created = []
        for kind in TYPE_KINDS:
            tid = self.default_type_id(kind)
            if tid in self.asset_types:
                continue
            self.asset_types[tid] = AssetType(
                id=tid, name=f"Default {kind}", kind=kind, version=1,
                description=f"Default asset type assigned to new {kind}s. Add parameters, sensors, conditions and "
                            "behaviors here to apply them to every bound instance.",
                updated_at=self.sim.now, changelog=[{"version": 1, "time": self.sim.now, "summary": "created (default)"}])
            created.append(tid)
        return created


DEFAULT_TYPE_PREFIX = "default-"


# --------------------------------------------------------------------------- helpers

def _finite(v: Any) -> float:
    return as_number(v, "value")


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


def _mapping_desc(m: dict) -> dict:
    keys = ("source", "ref", "aggregation", "expression", "value", "input_slot", "input_product_type_id",
            "weight_attribute", "when")
    return {k: m[k] for k in keys if m.get(k) is not None}


def _aggregate_inputs(m: dict, present: list, hub: IndustrialHub) -> Any:
    agg = m["aggregation"]
    vals = [v for _, v in present]
    if agg == "count":
        return len(vals)
    if not vals:
        return None
    if agg == "first":
        return copy.deepcopy(vals[0])
    if agg == "last":
        return copy.deepcopy(vals[-1])
    if agg in ("any", "all"):
        return (any if agg == "any" else all)(bool(v) for v in vals)
    if agg == "concat":
        return ",".join(str(v) for v in vals)
    if agg == "unique":
        return sorted({str(v) for v in vals})
    if agg == "majority":
        counts: dict[str, int] = {}
        for v in vals:
            counts[repr(v)] = counts.get(repr(v), 0) + 1
        best = max(counts.values())
        return next(v for v in vals if counts[repr(v)] == best)
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not nums:
        return None
    if agg == "avg":
        return sum(nums) / len(nums)
    if agg == "min":
        return min(nums)
    if agg == "max":
        return max(nums)
    if agg == "sum":
        return sum(nums)
    if agg == "weighted_avg":
        wsum = total = 0.0
        for p, v in present:
            w = hub._input_attr(p, m["weight_attribute"])
            if isinstance(v, (int, float)) and isinstance(w, (int, float)) and not isinstance(w, bool):
                wsum += w
                total += v * w
        return total / wsum if wsum else None
    return None


__all__ = ["IndustrialHub", "CONFIG_KEYS", "SAMPLE_PRIORITY", "TYPE_KINDS", "SECTIONS", "default_sync",
           "normalize_sync"]
