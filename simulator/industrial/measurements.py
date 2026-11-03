"""Sensor observations and the measurement store.

Every value that enters the system – simulated sensor samples, externally ingested telemetry and
computed composite measurements – becomes an :class:`Observation` with full context (asset,
measurement, unit, simulation time, source, quality, cycle, original timestamps). The store keeps
a bounded history per measurement, the latest *valid* value per measurement, and incremental
per-cycle aggregates (initial/final/avg/min/max/count) used for product attribute mappings and
the production-cycle dataset.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

QUALITIES = ("good", "out_of_range", "late", "out_of_order", "estimated", "bad")


@dataclass
class Observation:
    seq: int
    asset_id: str
    measurement_id: str
    kind: str                    # 'sensor' | 'composite'
    value: Any
    unit: str
    sim_time: float
    source: str                  # 'simulated' | 'external' | 'composite'
    quality: str = "good"
    raw_value: Any = None
    cycle_id: str | None = None
    source_timestamp: str | None = None
    received_at: float | None = None     # sim time at which the value was applied
    message_id: str | None = None
    product_id: str | None = None

    def to_dict(self) -> dict:
        return {"seq": self.seq, "asset_id": self.asset_id, "measurement_id": self.measurement_id,
                "kind": self.kind, "value": self.value, "raw_value": self.raw_value, "unit": self.unit,
                "sim_time": self.sim_time, "source": self.source, "quality": self.quality,
                "cycle_id": self.cycle_id, "source_timestamp": self.source_timestamp,
                "received_at": self.received_at, "message_id": self.message_id,
                "product_id": self.product_id}


@dataclass
class Aggregate:
    initial: Any = None
    final: Any = None
    min: float | None = None
    max: float | None = None
    total: float = 0.0
    count: int = 0
    numeric_count: int = 0
    unit: str = ""
    first_time: float | None = None
    last_time: float | None = None

    def add(self, value: Any, t: float, unit: str) -> None:
        if self.count == 0 or (self.first_time is not None and t < self.first_time):
            self.initial, self.first_time = value, t
        if self.last_time is None or t >= self.last_time:
            self.final, self.last_time = value, t
        self.count += 1
        self.unit = unit or self.unit
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.numeric_count += 1
            self.total += value
            self.min = value if self.min is None else min(self.min, value)
            self.max = value if self.max is None else max(self.max, value)

    def get(self, aggregation: str) -> Any:
        if aggregation in ("final", "latest"):
            return self.final
        if aggregation == "initial":
            return self.initial
        if aggregation == "avg":
            return self.total / self.numeric_count if self.numeric_count else None
        if aggregation == "min":
            return self.min
        if aggregation == "max":
            return self.max
        if aggregation == "count":
            return self.count
        if aggregation == "range":
            return (self.max - self.min) if self.numeric_count else None
        raise KeyError(aggregation)

    def to_dict(self) -> dict:
        return {"initial": self.initial, "final": self.final, "avg": self.get("avg"), "min": self.min,
                "max": self.max, "count": self.count, "unit": self.unit}


class MeasurementStore:
    def __init__(self, history_per_measurement: int = 5000) -> None:
        self.history_limit = history_per_measurement
        self.seq = 0
        self.history: dict[tuple[str, str], deque[Observation]] = {}
        self.latest: dict[tuple[str, str], Observation] = {}
        self.cycle_aggregates: dict[str, dict[str, Aggregate]] = {}
        self.total = 0

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def add(self, obs: Observation, *, update_latest: bool = True) -> None:
        key = (obs.asset_id, obs.measurement_id)
        hist = self.history.get(key)
        if hist is None:
            hist = self.history[key] = deque(maxlen=self.history_limit)
        hist.append(obs)
        self.total += 1
        valid = obs.quality in ("good", "estimated", "late")
        if update_latest and valid:
            cur = self.latest.get(key)
            if cur is None or obs.sim_time >= cur.sim_time:
                self.latest[key] = obs
        if obs.cycle_id and valid:
            aggs = self.cycle_aggregates.setdefault(obs.cycle_id, {})
            agg = aggs.get(obs.measurement_id)
            if agg is None:
                agg = aggs[obs.measurement_id] = Aggregate()
            agg.add(obs.value, obs.sim_time, obs.unit)

    def latest_value(self, asset_id: str, measurement_id: str) -> Any:
        obs = self.latest.get((asset_id, measurement_id))
        return obs.value if obs else None

    def latest_for(self, asset_id: str) -> dict[str, Observation]:
        return {mid: o for (aid, mid), o in self.latest.items() if aid == asset_id}

    def query(self, *, asset_id: str | None = None, measurement_id: str | None = None,
              since_seq: int | None = None, since_time: float | None = None, cycle_id: str | None = None,
              limit: int = 500) -> list[Observation]:
        sources: Iterable[deque[Observation]]
        if asset_id is not None and measurement_id is not None:
            sources = [self.history.get((asset_id, measurement_id), deque())]
        else:
            sources = [h for (aid, mid), h in self.history.items()
                       if (asset_id is None or aid == asset_id) and (measurement_id is None or mid == measurement_id)]
        out = [o for h in sources for o in h
               if (since_seq is None or o.seq > since_seq) and (since_time is None or o.sim_time >= since_time)
               and (cycle_id is None or o.cycle_id == cycle_id)]
        out.sort(key=lambda o: o.seq)
        return out[-limit:] if since_seq is None else out[:limit]

    def cycle_summary(self, cycle_id: str) -> dict[str, dict]:
        return {mid: agg.to_dict() for mid, agg in self.cycle_aggregates.get(cycle_id, {}).items()}

    def forget_asset(self, asset_id: str) -> None:
        for key in [k for k in self.history if k[0] == asset_id]:
            del self.history[key]
        for key in [k for k in self.latest if k[0] == asset_id]:
            del self.latest[key]


@dataclass
class SensorRuntime:
    """Mutable per-sensor simulation state (model state, filter memory, scheduling)."""
    value: float | None = None          # model state (before noise/filter)
    last_sample: float | None = None
    event_seq: int | None = None
    window: deque = field(default_factory=lambda: deque(maxlen=1000))
    ema: float | None = None
