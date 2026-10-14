"""Product attribute conditions used by conditional splitters and sink completion criteria.

A condition is ``{"attribute": str, "operator": str, "value": Any}``. Supported attributes:

* ``is_defective``, ``product_type_id``, ``quality_score``, ``lot_number``
* ``measurements.<name>`` and ``attributes.<name>``

If the attribute is missing (e.g. no quality score yet), the condition does *not* match and
the evaluation reports ``missing=True`` so callers can record it.
"""

from __future__ import annotations

from typing import Any

from .errors import ValidationError
from .models import Product

OPERATORS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "not_in", "exists")
DIRECT_ATTRIBUTES = ("is_defective", "product_type_id", "quality_score", "lot_number")
_NUMERIC_OPS = ("lt", "le", "gt", "ge")


def validate_condition(c: Any, where: str) -> dict:
    if not isinstance(c, dict):
        raise ValidationError(f"{where} must be an object")
    attr = c.get("attribute")
    if not isinstance(attr, str) or not (
        attr in DIRECT_ATTRIBUTES
        or (attr.startswith("measurements.") and len(attr) > 13)
        or (attr.startswith("attributes.") and len(attr) > 11)
    ):
        raise ValidationError(
            f"{where}.attribute must be one of {DIRECT_ATTRIBUTES} or 'measurements.<name>' / 'attributes.<name>'"
        )
    op = c.get("operator", "eq")
    if op not in OPERATORS:
        raise ValidationError(f"{where}.operator must be one of {OPERATORS}")
    value = c.get("value")
    if op in _NUMERIC_OPS and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ValidationError(f"{where}: operator '{op}' requires a numeric value")
    if op in ("in", "not_in") and not isinstance(value, list):
        raise ValidationError(f"{where}: operator '{op}' requires a list value")
    if attr == "is_defective" and op in ("eq", "ne") and not isinstance(value, bool):
        raise ValidationError(f"{where}: is_defective must be compared with a boolean")
    out = {"attribute": attr, "operator": op, "value": value}
    return out


def get_attribute(product: Product, path: str) -> tuple[bool, Any]:
    if path == "is_defective":
        return True, product.is_defective
    if path == "product_type_id":
        return True, product.product_type_id
    if path == "lot_number":
        return True, product.lot_number
    if path == "quality_score":
        return (product.quality_score is not None), product.quality_score
    if path.startswith("measurements."):
        key = path[len("measurements."):]
        return (key in product.measurements), product.measurements.get(key)
    if path.startswith("attributes."):
        key = path[len("attributes."):]
        return (key in product.attributes), product.attributes.get(key)
    return False, None


def evaluate(condition: dict, product: Product) -> tuple[bool, bool]:
    """Return ``(matched, missing_attribute)``."""
    found, actual = get_attribute(product, condition["attribute"])
    op = condition["operator"]
    if op == "exists":
        return found, False
    if not found:
        return False, True
    expected = condition["value"]
    try:
        if op == "eq":
            return actual == expected, False
        if op == "ne":
            return actual != expected, False
        if op == "in":
            return actual in expected, False
        if op == "not_in":
            return actual not in expected, False
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False, True
        if op == "lt":
            return actual < expected, False
        if op == "le":
            return actual <= expected, False
        if op == "gt":
            return actual > expected, False
        if op == "ge":
            return actual >= expected, False
    except TypeError:
        return False, True
    return False, False
