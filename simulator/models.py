"""Typed domain models: product types, recipes, orders, products, histories, cycles, records."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import ValidationError
from .ids import validate_id

MAX_RECIPE_INPUTS = 5
MAX_RECIPE_OUTPUTS = 3


def _require_str(d: dict, key: str, *, allow_empty: bool = False) -> str:
    value = d.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValidationError(f"'{key}' must be a non-empty string")
    return value


def _require_dict(value: Any, key: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(f"'{key}' must be an object")
    return dict(value)


def require_positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"'{key}' must be a positive integer, got {value!r}")
    return value


def require_positive_number(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValidationError(f"'{key}' must be a positive number, got {value!r}")
    if value != value or value == float("inf"):
        raise ValidationError(f"'{key}' must be finite")
    return float(value)


# --------------------------------------------------------------------------- product types


@dataclass
class ProductType:
    id: str
    name: str
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    attributes: list[dict[str, Any]] = field(default_factory=list)   # product attribute definitions

    @classmethod
    def from_dict(cls, d: dict) -> "ProductType":
        if not isinstance(d, dict):
            raise ValidationError("product type must be an object")
        from .industrial.definitions import normalize_attribute_definitions
        return cls(
            id=validate_id(d.get("id"), "product type id"),
            name=_require_str(d, "name"),
            description=str(d.get("description") or ""),
            metadata=_require_dict(d.get("metadata"), "metadata"),
            attributes=normalize_attribute_definitions(d.get("attributes")),
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description,
                "metadata": copy.deepcopy(self.metadata), "attributes": copy.deepcopy(self.attributes)}


# --------------------------------------------------------------------------- recipes

DEFECT_POLICIES = ("independent", "any_defective_input")


@dataclass
class RecipeItem:
    product_type_id: str
    quantity: int
    is_byproduct: bool = False
    attribute_mappings: list[dict[str, Any]] = field(default_factory=list)   # outputs only

    def to_dict(self) -> dict:
        d = {"product_type_id": self.product_type_id, "quantity": self.quantity, "is_byproduct": self.is_byproduct}
        if self.attribute_mappings:
            d["attribute_mappings"] = copy.deepcopy(self.attribute_mappings)
        return d


@dataclass
class Recipe:
    id: str
    name: str
    inputs: list[RecipeItem]
    outputs: list[RecipeItem]
    processing_time_s: float
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    defect_policy: str = "any_defective_input"

    @classmethod
    def from_dict(cls, d: dict, known_types: set[str] | None = None) -> "Recipe":
        if not isinstance(d, dict):
            raise ValidationError("recipe must be an object")

        def items(key: str, lo: int, hi: int, allow_byproduct: bool) -> list[RecipeItem]:
            raw = d.get(key)
            if not isinstance(raw, list) or not (lo <= len(raw) <= hi):
                raise ValidationError(f"recipe '{key}' must be a list of {lo}..{hi} slots")
            result = []
            for i, it in enumerate(raw):
                if not isinstance(it, dict):
                    raise ValidationError(f"recipe {key}[{i}] must be an object")
                ptid = validate_id(it.get("product_type_id"), f"{key}[{i}].product_type_id")
                if known_types is not None and ptid not in known_types:
                    raise ValidationError(f"recipe {key}[{i}] references unknown product type '{ptid}'")
                qty = require_positive_int(it.get("quantity"), f"{key}[{i}].quantity")
                byp = bool(it.get("is_byproduct", False))
                if byp and not allow_byproduct:
                    raise ValidationError("only outputs may be by-products")
                mappings = []
                if it.get("attribute_mappings"):
                    if not allow_byproduct:
                        raise ValidationError(f"recipe {key}[{i}]: attribute mappings are defined on outputs only")
                    from .industrial.definitions import normalize_mappings
                    mappings = normalize_mappings(it["attribute_mappings"], f"{key}[{i}].attribute_mappings")
                result.append(RecipeItem(ptid, qty, byp, mappings))
            return result

        policy = d.get("defect_policy", "any_defective_input")
        if policy not in DEFECT_POLICIES:
            raise ValidationError(f"defect_policy must be one of {DEFECT_POLICIES}")
        return cls(
            id=validate_id(d.get("id"), "recipe id"),
            name=_require_str(d, "name"),
            inputs=items("inputs", 1, MAX_RECIPE_INPUTS, False),
            outputs=items("outputs", 1, MAX_RECIPE_OUTPUTS, True),
            processing_time_s=require_positive_number(d.get("processing_time_s"), "processing_time_s"),
            description=str(d.get("description") or ""),
            parameters=_require_dict(d.get("parameters"), "parameters"),
            defect_policy=policy,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "inputs": [i.to_dict() for i in self.inputs],
            "outputs": [o.to_dict() for o in self.outputs],
            "processing_time_s": self.processing_time_s,
            "parameters": copy.deepcopy(self.parameters),
            "defect_policy": self.defect_policy,
        }

    def referenced_types(self) -> set[str]:
        return {i.product_type_id for i in self.inputs} | {o.product_type_id for o in self.outputs}


# --------------------------------------------------------------------------- production orders


@dataclass
class ProductionOrder:
    id: str
    name: str
    description: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ProductionOrder":
        if not isinstance(d, dict):
            raise ValidationError("production order must be an object")
        return cls(
            id=validate_id(d.get("id"), "production order id"),
            name=_require_str(d, "name"),
            description=str(d.get("description") or ""),
            attributes=_require_dict(d.get("attributes"), "attributes"),
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description,
                "attributes": copy.deepcopy(self.attributes)}


# --------------------------------------------------------------------------- products


class ProductStatus(str, Enum):
    QUEUED = "queued"          # waiting in an asset buffer (producer output, splitter, transformer)
    IN_TRANSIT = "in_transit"  # on a conveyor (moving or waiting at its head)
    RESERVED = "reserved"      # reserved by an active transformer cycle
    CONSUMED = "consumed"      # consumed by a completed transformation (terminal)
    COMPLETED = "completed"    # accepted by a sink and completion criteria satisfied (terminal)
    STORED = "stored"          # accepted by a sink but completion criteria not met (terminal)


ACTIVE_STATUSES = {ProductStatus.QUEUED, ProductStatus.IN_TRANSIT, ProductStatus.RESERVED}
TERMINAL_STATUSES = {ProductStatus.CONSUMED, ProductStatus.COMPLETED, ProductStatus.STORED}


@dataclass
class HistoryEntry:
    """One processing record: logical schema {case_id, activity, timestamp_start, timestamp_end, attributes}.

    ``timestamp_end`` is ``None`` while the operation is still active. Once completed the
    entry is immutable.
    """

    entry_id: str
    seq: int
    product_id: str
    case_id: str
    activity: str
    activity_type: str
    timestamp_start: float
    timestamp_end: float | None = None
    cycle_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.timestamp_end is not None

    def complete(self, end: float, **attributes: Any) -> None:
        if self.timestamp_end is not None:
            raise RuntimeError(f"history entry {self.entry_id} is already completed and immutable")
        self.timestamp_end = end
        self.attributes.update(attributes)

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id, "seq": self.seq, "product_id": self.product_id,
            "case_id": self.case_id, "activity": self.activity, "activity_type": self.activity_type,
            "timestamp_start": self.timestamp_start, "timestamp_end": self.timestamp_end,
            "status": "completed" if self.completed else "active",
            "cycle_id": self.cycle_id, "attributes": copy.deepcopy(self.attributes),
        }


@dataclass
class Location:
    asset_id: str
    area: str            # 'output_buffer', 'input_buffer', 'conveyor', 'splitter_queue', 'cycle', 'sink'
    port: str | None = None

    def to_dict(self) -> dict:
        return {"asset_id": self.asset_id, "area": self.area, "port": self.port}


@dataclass
class Product:
    id: str
    label: str
    product_type_id: str
    lot_number: str
    production_timestamp: float
    status: ProductStatus
    location: Location | None
    is_defective: bool
    production_order_ids: list[str]
    origin_cycle_id: str
    parent_ids: list[str] = field(default_factory=list)
    quality_score: float | None = None
    measurements: dict[str, float] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    history: list[HistoryEntry] = field(default_factory=list)
    terminal_at: float | None = None
    attribute_provenance: dict[str, dict] = field(default_factory=dict)
    external_id: str | None = None

    @property
    def primary_order_id(self) -> str:
        return self.production_order_ids[0]

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def summary(self) -> dict:
        return {
            "id": self.id, "label": self.label, "product_type_id": self.product_type_id,
            "lot_number": self.lot_number, "production_timestamp": self.production_timestamp,
            "status": self.status.value,
            "location": self.location.to_dict() if self.location else None,
            "is_defective": self.is_defective, "quality_score": self.quality_score,
            "production_order_ids": list(self.production_order_ids),
            "origin_cycle_id": self.origin_cycle_id, "parent_ids": list(self.parent_ids),
            "terminal_at": self.terminal_at, "attributes": copy.deepcopy(self.attributes),
            "external_id": self.external_id,
        }


# --------------------------------------------------------------------------- cycles & records


@dataclass
class ProductionCycle:
    id: str
    asset_id: str
    kind: str                          # 'producer_batch' | 'transformation'
    status: str                        # 'active' | 'completed' | 'cancelled'
    started_at: float
    lot_number: str
    production_order_ids: list[str]
    config_snapshot: dict[str, Any]
    config_version: int
    ended_at: float | None = None
    scheduled_end: float | None = None
    recipe_id: str | None = None
    input_ids: list[str] = field(default_factory=list)
    output_ids: list[str] = field(default_factory=list)
    defect_count: int = 0
    end_reason: str | None = None
    measurements: dict[str, dict] = field(default_factory=dict)      # per-cycle measurement aggregates
    source: str = "simulation"                                       # 'simulation' | 'external'
    external_cycle_id: str | None = None
    external_timestamps: dict[str, Any] = field(default_factory=dict)
    exceptions: list[str] = field(default_factory=list)              # reconciliation exceptions (lenient)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "asset_id": self.asset_id, "kind": self.kind, "status": self.status,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "scheduled_end": self.scheduled_end, "lot_number": self.lot_number,
            "production_order_ids": list(self.production_order_ids),
            "recipe_id": self.recipe_id, "config_version": self.config_version,
            "config_snapshot": copy.deepcopy(self.config_snapshot),
            "input_ids": list(self.input_ids), "output_ids": list(self.output_ids),
            "defect_count": self.defect_count, "end_reason": self.end_reason,
            "measurements": copy.deepcopy(self.measurements), "source": self.source,
            "external_cycle_id": self.external_cycle_id,
            "external_timestamps": copy.deepcopy(self.external_timestamps),
            "exceptions": list(self.exceptions),
        }


@dataclass
class MachineEvent:
    event_id: str
    seq: int
    event_type: str
    asset_id: str
    activity: str
    timestamp: float
    timestamp_start: float | None = None
    timestamp_end: float | None = None
    production_order_ids: list[str] = field(default_factory=list)
    cycle_id: str | None = None
    input_product_ids: list[str] = field(default_factory=list)
    output_product_ids: list[str] = field(default_factory=list)
    input_quantity: int | None = None
    output_quantity: int | None = None
    defect_count: int | None = None
    config: dict[str, Any] = field(default_factory=dict)
    readings: dict[str, Any] = field(default_factory=dict)
    status: str | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id, "seq": self.seq, "event_type": self.event_type,
            "asset_id": self.asset_id, "activity": self.activity, "timestamp": self.timestamp,
            "timestamp_start": self.timestamp_start, "timestamp_end": self.timestamp_end,
            "production_order_ids": list(self.production_order_ids), "cycle_id": self.cycle_id,
            "input_product_ids": list(self.input_product_ids),
            "output_product_ids": list(self.output_product_ids),
            "input_quantity": self.input_quantity, "output_quantity": self.output_quantity,
            "defect_count": self.defect_count, "config": copy.deepcopy(self.config),
            "readings": copy.deepcopy(self.readings), "status": self.status,
            "error": self.error, "details": copy.deepcopy(self.details),
        }


@dataclass
class OperationalInterval:
    """A period during which an asset operated in one state under particular conditions."""

    asset_id: str
    state: str
    start: float
    end: float | None = None
    reason: str | None = None
    conditions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"asset_id": self.asset_id, "state": self.state, "start": self.start,
                "end": self.end, "reason": self.reason, "conditions": copy.deepcopy(self.conditions)}


@dataclass
class ConfigChange:
    change_id: str
    asset_id: str
    timestamp: float
    version: int
    changes: dict[str, dict[str, Any]]
    effective: str

    def to_dict(self) -> dict:
        return {"change_id": self.change_id, "asset_id": self.asset_id, "timestamp": self.timestamp,
                "version": self.version, "changes": copy.deepcopy(self.changes),
                "effective": self.effective}
