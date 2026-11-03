"""Reusable asset types (templates) and per-instance bindings.

An :class:`AssetType` bundles a kind, default configuration and an industrial profile
(parameters, sensors, composites, working conditions, behaviour models). Updating a type bumps its
version; bound instances keep the *template snapshot* of the version they were last synchronised
with, so template changes never alter running instances implicitly. Instances become
"outdated" and can be synchronised explicitly (with a dry-run diff) while their overrides are
preserved.

An :class:`AssetBinding` holds, per asset:

* ``type_id`` / ``type_version`` / ``template`` – the bound template snapshot (or ``None``),
* ``overrides`` – instance deviations: ``config`` values, ``parameters`` values and partial
  patches for template ``sensors``/``composites``/``conditions``/``behaviors`` (by id),
* ``local`` – instance-only profile items that are not part of the template.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from ..errors import ValidationError
from ..ids import validate_id
from .definitions import (SECTION_KEY, SECTIONS, apply_item_override, coerce_value, empty_profile,
                          normalize_profile)

TYPE_KINDS = ("producer", "transformer", "conveyor", "splitter", "sink")
OVERRIDE_SECTIONS = ("sensors", "composites", "conditions", "behaviors")


@dataclass
class AssetType:
    id: str
    name: str
    kind: str
    version: int = 1
    description: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, list] = field(default_factory=empty_profile)
    updated_at: float = 0.0
    changelog: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Any) -> "AssetType":
        if not isinstance(d, dict):
            raise ValidationError("asset type must be an object")
        kind = d.get("kind")
        if kind not in TYPE_KINDS:
            raise ValidationError(f"asset type kind must be one of {list(TYPE_KINDS)}")
        name = d.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("asset type 'name' must be a non-empty string")
        cfg = d.get("config") or {}
        if not isinstance(cfg, dict):
            raise ValidationError("asset type 'config' must be an object")
        for k in ("id", "name", "kind"):
            if k in cfg:
                raise ValidationError(f"asset type config cannot contain '{k}'")
        version = d.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValidationError("asset type 'version' must be a positive integer")
        return cls(id=validate_id(d.get("id"), "asset type id"), name=name.strip(), kind=kind, version=version,
                   description=str(d.get("description") or ""), config=copy.deepcopy(cfg),
                   profile=normalize_profile(d.get("profile")), updated_at=float(d.get("updated_at") or 0.0),
                   changelog=list(d.get("changelog") or []))

    def template(self) -> dict:
        return {"config": copy.deepcopy(self.config), "profile": copy.deepcopy(self.profile)}

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "version": self.version,
                "description": self.description, "config": copy.deepcopy(self.config),
                "profile": copy.deepcopy(self.profile), "updated_at": self.updated_at,
                "changelog": copy.deepcopy(self.changelog[-50:])}


def empty_overrides() -> dict:
    return {"config": {}, "parameters": {}, **{s: {} for s in OVERRIDE_SECTIONS}}


@dataclass
class AssetBinding:
    type_id: str | None = None
    type_version: int | None = None
    template: dict = field(default_factory=lambda: {"config": {}, "profile": empty_profile()})
    resolved_template_config: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=empty_overrides)
    local: dict = field(default_factory=empty_profile)

    def to_dict(self) -> dict:
        return {"type_id": self.type_id, "type_version": self.type_version,
                "template": copy.deepcopy(self.template), "overrides": copy.deepcopy(self.overrides),
                "local": copy.deepcopy(self.local)}

    @classmethod
    def from_dict(cls, d: Any) -> "AssetBinding":
        if d is None:
            return cls()
        if not isinstance(d, dict):
            raise ValidationError("industrial binding must be an object")
        b = cls()
        b.type_id = d.get("type_id")
        b.type_version = d.get("type_version")
        tpl = d.get("template") or {}
        if not isinstance(tpl, dict):
            raise ValidationError("binding template must be an object")
        b.template = {"config": dict(tpl.get("config") or {}), "profile": normalize_profile(tpl.get("profile"))}
        ov = d.get("overrides") or {}
        if not isinstance(ov, dict):
            raise ValidationError("overrides must be an object")
        unknown = set(ov) - set(empty_overrides())
        if unknown:
            raise ValidationError(f"unknown override section(s) {sorted(unknown)}")
        b.overrides = empty_overrides()
        for k, v in ov.items():
            if not isinstance(v, dict):
                raise ValidationError(f"overrides.{k} must be an object")
            b.overrides[k] = copy.deepcopy(v)
        b.local = normalize_profile(d.get("local"), "local")
        return b


def effective_profile(binding: AssetBinding) -> tuple[dict, list[str]]:
    """Merge template, overrides and local items. Returns (profile, problems).

    Problems list overrides that no longer apply (e.g. the template item was removed); they are
    reported by the sync diff and dropped when synchronising.
    """
    problems: list[str] = []
    tpl = binding.template["profile"]
    out = empty_profile()
    for s in SECTIONS:
        key = SECTION_KEY[s]
        tpl_keys = {it[key] for it in tpl[s]}
        for it in tpl[s]:
            patch = binding.overrides.get(s, {}).get(it[key]) if s in OVERRIDE_SECTIONS else None
            out[s].append(apply_item_override(s, it, patch, f"overrides.{s}.{it[key]}") if patch else
                          copy.deepcopy(it))
        if s in OVERRIDE_SECTIONS:
            for k in binding.overrides.get(s, {}):
                if k not in tpl_keys:
                    problems.append(f"override for {s[:-1]} '{k}' has no template item")
        for it in binding.local[s]:
            if it[key] in tpl_keys:
                raise ValidationError(f"local {s[:-1]} '{it[key]}' clashes with a template item; use an override")
            out[s].append(copy.deepcopy(it))
    params = {p["name"]: p for p in out["parameters"]}
    for name in binding.overrides.get("parameters", {}):
        if name not in params:
            problems.append(f"parameter value for '{name}' has no definition")
    return out, problems


def parameter_values(profile: dict, binding: AssetBinding) -> dict[str, Any]:
    vals = {}
    ov = binding.overrides.get("parameters", {})
    for p in profile["parameters"]:
        vals[p["name"]] = ov.get(p["name"], p["default"])
    return vals


def validate_parameter_values(profile: dict, values: dict, *, typed_names: set[str]) -> dict:
    """Validate a parameter value patch against definitions. ``typed_names`` are template params."""
    if not isinstance(values, dict) or not values:
        raise ValidationError("parameter patch must be a non-empty object")
    defs = {p["name"]: p for p in profile["parameters"]}
    out = {}
    for name, value in values.items():
        d = defs.get(name)
        if d is None:
            raise ValidationError(f"unknown parameter '{name}'")
        if name in typed_names and not d["overridable"]:
            raise ValidationError(f"parameter '{name}' is not overridable per instance; change the asset type")
        out[name] = coerce_value(d, value, f"parameter '{name}'")
        if out[name] is None:
            raise ValidationError(f"parameter '{name}' cannot be null")
    return out


def diff_profiles(old: dict, new: dict) -> dict:
    out = {}
    for s in SECTIONS:
        key = SECTION_KEY[s]
        a = {it[key]: it for it in old.get(s, [])}
        b = {it[key]: it for it in new.get(s, [])}
        added = sorted(set(b) - set(a))
        removed = sorted(set(a) - set(b))
        changed = {}
        for k in sorted(set(a) & set(b)):
            if a[k] != b[k]:
                changed[k] = {f: {"old": a[k].get(f), "new": b[k].get(f)}
                              for f in sorted(set(a[k]) | set(b[k])) if a[k].get(f) != b[k].get(f)}
        if added or removed or changed:
            out[s] = {"added": added, "removed": removed, "changed": changed}
    return out
