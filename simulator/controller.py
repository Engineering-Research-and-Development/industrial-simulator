"""Wall-clock pacing of the simulation: start/pause/resume/stop and the accelerator.

Simulation time is derived from an *anchor*: ``sim_time = anchor_sim + (wall - anchor_wall) * acceleration``.
Every acceleration change or resume re-anchors at the current simulation time, so changing the
accelerator never resets the clock, never duplicates events, and never changes the simulated
duration of an active operation (all durations are fixed in simulation time when scheduled).

The controller is thread-safe: a background thread calls :meth:`tick` periodically while API
calls hold the same lock.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .engine import Simulation
from .errors import ConflictError, ValidationError

MAX_ACCELERATION = 10_000.0


class SimulationController:
    def __init__(self, sim: Simulation, *, time_fn: Callable[[], float] = time.monotonic,
                 acceleration: float = 1.0) -> None:
        self.sim = sim
        self.time_fn = time_fn
        self.lock = threading.RLock()
        self.status = "idle"   # idle | running | paused | stopped
        self.acceleration = self._check_acc(acceleration)
        self._anchor_wall = time_fn()
        self._anchor_sim = sim.now
        self._started_wall: float | None = None
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self.on_tick: list[Callable[[], None]] = []

    @staticmethod
    def _check_acc(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0 < value <= MAX_ACCELERATION):
            raise ValidationError(f"acceleration must be within (0, {MAX_ACCELERATION:g}]")
        return float(value)

    # ------------------------------------------------------------------ control
    def _reanchor(self) -> None:
        self._anchor_wall = self.time_fn()
        self._anchor_sim = self.sim.now

    def start(self) -> None:
        with self.lock:
            if self.status == "running":
                return
            if self.status == "stopped":
                raise ConflictError("simulation was stopped; reset it before starting again")
            if self.status == "paused":
                return self.resume()
            self.sim.start()
            self._started_wall = self.time_fn()
            self._reanchor()
            self.status = "running"

    def pause(self) -> None:
        with self.lock:
            if self.status != "running":
                raise ConflictError(f"cannot pause while {self.status}")
            self.tick()
            self.status = "paused"

    def resume(self) -> None:
        with self.lock:
            if self.status != "paused":
                raise ConflictError(f"cannot resume while {self.status}")
            self._reanchor()
            self.status = "running"

    def stop(self) -> None:
        with self.lock:
            if self.status == "running":
                self.tick()
            self.status = "stopped"

    def set_acceleration(self, factor: float) -> None:
        with self.lock:
            factor = self._check_acc(factor)
            if self.status == "running":
                self.tick()           # advance with the old factor up to "now"
            self.acceleration = factor
            self._reanchor()          # continue from the current simulation time

    def replace_simulation(self, sim: Simulation, status: str = "idle") -> None:
        with self.lock:
            self.sim = sim
            self.status = status
            self._reanchor()

    def step(self, seconds: float) -> None:
        """Advance simulation time manually (only while paused or idle)."""
        with self.lock:
            if self.status == "running":
                raise ConflictError("pause the simulation before stepping manually")
            if self.status == "stopped":
                raise ConflictError("simulation is stopped; reset it first")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
                raise ValidationError("step seconds must be positive")
            if self.status == "idle":
                self.sim.start()
                self.status = "paused"
            self.sim.run_until(self.sim.now + float(seconds))

    # ------------------------------------------------------------------ pacing
    def target_time(self) -> float:
        return self._anchor_sim + (self.time_fn() - self._anchor_wall) * self.acceleration

    def tick(self) -> int:
        with self.lock:
            if self.status != "running":
                return 0
            processed = self.sim.run_until(self.target_time())
            for cb in list(self.on_tick):
                try:
                    cb()
                except Exception:
                    pass
            return processed

    def info(self) -> dict:
        with self.lock:
            return {"status": self.status, "acceleration": self.acceleration, "sim_time": self.sim.now,
                    "wall_time_since_start": (self.time_fn() - self._started_wall) if self._started_wall else 0.0,
                    "wall_clock": time.time(), "next_event_time": self.sim.next_event_time()}

    # ------------------------------------------------------------------ background thread
    def start_background(self, period_s: float = 0.02) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._shutdown.clear()

        def loop() -> None:
            while not self._shutdown.is_set():
                try:
                    self.tick()
                except Exception:  # keep the pacing thread alive; errors are visible via the API
                    import traceback
                    traceback.print_exc()
                    with self.lock:
                        self.status = "paused"
                self._shutdown.wait(period_s)

        self._thread = threading.Thread(target=loop, name="sim-pacer", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._shutdown.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
