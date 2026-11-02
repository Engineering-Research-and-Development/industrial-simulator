"""Speed → processing-time and speed → quality models, plus the quality → defect policy."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .errors import ValidationError

DEFAULT_SPEED_POINTS: list[tuple[float, float]] = [(1.0, 2.0), (50.0, 1.0), (100.0, 0.5)]


def validate_speed(speed_pct: object) -> float:
    if isinstance(speed_pct, bool) or not isinstance(speed_pct, (int, float)) or not (1 <= speed_pct <= 100):
        raise ValidationError(f"operating speed must be between 1 and 100 (%), got {speed_pct!r}")
    return float(speed_pct)


def interpolate(points: list[tuple[float, float]], x: float) -> float:
    """Piecewise-linear interpolation, clamped to the outer points."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


@dataclass
class SpeedModel:
    points: list[tuple[float, float]] = field(default_factory=lambda: list(DEFAULT_SPEED_POINTS))

    @classmethod
    def from_dict(cls, d: dict | None) -> "SpeedModel":
        if not d:
            return cls()
        raw = d.get("points") if isinstance(d, dict) else None
        if not isinstance(raw, list) or len(raw) < 2:
            raise ValidationError("speed_model.points must be a list of at least two [speed_pct, multiplier] pairs")
        pts: list[tuple[float, float]] = []
        for p in raw:
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                raise ValidationError("speed_model points must be [speed_pct, multiplier] pairs")
            s, m = float(p[0]), float(p[1])
            if not (1 <= s <= 100) or m <= 0:
                raise ValidationError("speed_model points need speed in [1,100] and multiplier > 0")
            pts.append((s, m))
        if any(b[0] <= a[0] for a, b in zip(pts, pts[1:])):
            raise ValidationError("speed_model points must have strictly increasing speeds")
        return cls(pts)

    def to_dict(self) -> dict:
        return {"points": [list(p) for p in self.points]}

    def multiplier(self, speed_pct: float) -> float:
        return interpolate(self.points, speed_pct)


@dataclass
class QualityModel:
    """Default quality curve.

    * 1 %..50 %: linear from ``q_at_1`` to ``q_at_50``.
    * 50 %..100 %: normalised logistic (sigmoid-like) from ``q_at_50`` to ``q_at_100``;
      ``steepness`` controls how sharply quality drops around 75 % speed.
    """

    q_at_1: float = 1.0
    q_at_50: float = 0.999
    q_at_100: float = 0.99
    steepness: float = 10.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "QualityModel":
        if not d:
            return cls()
        if not isinstance(d, dict):
            raise ValidationError("quality_model must be an object")
        base = cls()
        try:
            m = cls(
                q_at_1=float(d.get("q_at_1", base.q_at_1)),
                q_at_50=float(d.get("q_at_50", base.q_at_50)),
                q_at_100=float(d.get("q_at_100", base.q_at_100)),
                steepness=float(d.get("steepness", base.steepness)),
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"invalid quality_model: {exc}") from exc
        for name in ("q_at_1", "q_at_50", "q_at_100"):
            v = getattr(m, name)
            if not (0.0 <= v <= 1.0):
                raise ValidationError(f"quality_model.{name} must be within [0, 1]")
        if not (0 < m.steepness <= 100):
            raise ValidationError("quality_model.steepness must be within (0, 100]")
        return m

    def to_dict(self) -> dict:
        return {"q_at_1": self.q_at_1, "q_at_50": self.q_at_50, "q_at_100": self.q_at_100,
                "steepness": self.steepness}

    def quality(self, speed_pct: float) -> float:
        s = min(100.0, max(1.0, speed_pct))
        if s <= 50.0:
            return self.q_at_1 + (self.q_at_50 - self.q_at_1) * (s - 1.0) / 49.0
        x = (s - 50.0) / 50.0
        k = self.steepness

        def sig(z: float) -> float:
            return 1.0 / (1.0 + math.exp(-z))

        lo, hi = sig(-k / 2), sig(k / 2)
        g = (sig(k * (x - 0.5)) - lo) / (hi - lo)
        return self.q_at_50 + (self.q_at_100 - self.q_at_50) * g


def defect_probability(quality: float) -> float:
    """Default (configurable by replacement) quality → defect mapping: p = 1 - q."""
    return min(1.0, max(0.0, 1.0 - quality))
