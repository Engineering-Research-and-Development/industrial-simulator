"""Small unit-conversion table used when ingesting external measurements."""

from __future__ import annotations

from ..errors import ValidationError

# unit -> (dimension, factor to the dimension's base unit, offset); value_base = value * factor + offset
_UNITS: dict[str, tuple[str, float, float]] = {
    "K": ("temperature", 1.0, 0.0),
    "°C": ("temperature", 1.0, 273.15), "C": ("temperature", 1.0, 273.15), "degC": ("temperature", 1.0, 273.15),
    "°F": ("temperature", 5.0 / 9.0, 273.15 - 32.0 * 5.0 / 9.0), "F": ("temperature", 5.0 / 9.0, 273.15 - 32.0 * 5.0 / 9.0),
    "degF": ("temperature", 5.0 / 9.0, 273.15 - 32.0 * 5.0 / 9.0),
    "Pa": ("pressure", 1.0, 0.0), "kPa": ("pressure", 1e3, 0.0), "MPa": ("pressure", 1e6, 0.0),
    "bar": ("pressure", 1e5, 0.0), "mbar": ("pressure", 100.0, 0.0), "psi": ("pressure", 6894.757293168, 0.0),
    "m": ("length", 1.0, 0.0), "mm": ("length", 1e-3, 0.0), "cm": ("length", 1e-2, 0.0), "um": ("length", 1e-6, 0.0),
    "µm": ("length", 1e-6, 0.0), "in": ("length", 0.0254, 0.0),
    "kg": ("mass", 1.0, 0.0), "g": ("mass", 1e-3, 0.0), "t": ("mass", 1e3, 0.0), "lb": ("mass", 0.45359237, 0.0),
    "s": ("time", 1.0, 0.0), "ms": ("time", 1e-3, 0.0), "min": ("time", 60.0, 0.0), "h": ("time", 3600.0, 0.0),
    "rpm": ("rotation", 1.0, 0.0), "Hz": ("frequency", 1.0, 0.0), "kHz": ("frequency", 1e3, 0.0),
    "%": ("ratio", 0.01, 0.0), "ratio": ("ratio", 1.0, 0.0),
    "W": ("power", 1.0, 0.0), "kW": ("power", 1e3, 0.0),
    "V": ("voltage", 1.0, 0.0), "mV": ("voltage", 1e-3, 0.0), "A": ("current", 1.0, 0.0), "mA": ("current", 1e-3, 0.0),
    "mm/s": ("velocity", 1e-3, 0.0), "m/s": ("velocity", 1.0, 0.0),
    "L/min": ("flow", 1.0, 0.0), "m3/h": ("flow", 1000.0 / 60.0, 0.0),
}


def convert(value: float, from_unit: str | None, to_unit: str | None) -> float:
    """Convert ``value`` between units; identical/blank units are a no-op."""
    if not from_unit or not to_unit or from_unit == to_unit:
        return value
    a, b = _UNITS.get(from_unit), _UNITS.get(to_unit)
    if a is None or b is None:
        raise ValidationError(f"cannot convert from unit '{from_unit}' to '{to_unit}' (unknown unit)")
    if a[0] != b[0]:
        raise ValidationError(f"cannot convert {a[0]} unit '{from_unit}' to {b[0]} unit '{to_unit}'")
    base = value * a[1] + a[2]
    return (base - b[2]) / b[1]


def known_units() -> list[str]:
    return sorted(_UNITS)
