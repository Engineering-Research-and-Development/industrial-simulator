"""Deterministic discrete-event scheduler.

Ordering policy for events with identical simulation timestamps:

1. ``priority`` ascending (see ``Priority``): completions/arrivals first, then producer
   generation ticks, then "wake" evaluations (push/pull attempts).
2. Insertion sequence number ascending (FIFO among equal priorities).

The ordering is therefore a total order and fully deterministic for a given input.
Events are plain tuples so that the scheduler (and the whole simulation) can be pickled
for checkpointing.
"""

from __future__ import annotations

import heapq
from typing import Any, NamedTuple


class Priority:
    COMPLETION = 10      # transformer cycle completions
    ARRIVAL = 20         # conveyor arrivals
    PRODUCER_TICK = 30   # producer batch generation
    SAMPLE = 35          # simulated sensor sampling and working-condition evaluation
    WAKE = 40            # asset re-evaluation (try to push / pull / start cycles)


class ScheduledEvent(NamedTuple):
    time: float
    priority: int
    seq: int
    kind: str
    target: str
    payload: Any


class Scheduler:
    def __init__(self) -> None:
        self._heap: list[ScheduledEvent] = []
        self._seq = 0
        self._cancelled: set[int] = set()

    def schedule(self, time: float, priority: int, kind: str, target: str, payload: Any = None) -> int:
        self._seq += 1
        heapq.heappush(self._heap, ScheduledEvent(time, priority, self._seq, kind, target, payload))
        return self._seq

    def cancel(self, seq: int | None) -> None:
        if seq is not None:
            self._cancelled.add(seq)

    def _discard_cancelled(self) -> None:
        while self._heap and self._heap[0].seq in self._cancelled:
            ev = heapq.heappop(self._heap)
            self._cancelled.discard(ev.seq)

    def peek_time(self) -> float | None:
        self._discard_cancelled()
        return self._heap[0].time if self._heap else None

    def pop(self) -> ScheduledEvent | None:
        self._discard_cancelled()
        return heapq.heappop(self._heap) if self._heap else None

    def pending(self) -> list[ScheduledEvent]:
        return sorted(e for e in self._heap if e.seq not in self._cancelled)

    def __len__(self) -> int:
        return sum(1 for e in self._heap if e.seq not in self._cancelled)
