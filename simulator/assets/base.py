"""Common asset behaviour: identity, ports, configuration, operational state tracking."""

from __future__ import annotations

import copy
from collections import deque
from typing import TYPE_CHECKING, Any, Iterable

from ..errors import ConflictError, ValidationError
from ..models import Location, OperationalInterval, Product, ProductStatus

if TYPE_CHECKING:
    from ..engine import Simulation
    from .conveyor import Conveyor

# Operational states shared by all assets.
IDLE = "idle"
RUNNING = "running"
PROCESSING = "processing"
BLOCKED = "blocked"
STOPPED = "stopped"
FAILED = "failed"
UNCONFIGURED = "unconfigured"

INITIAL_STATES = ("running", "stopped")


def parse_initial_state(raw: dict) -> str:
    value = raw.get("initial_state", "running")
    if value not in INITIAL_STATES:
        raise ValidationError(f"initial_state must be one of {INITIAL_STATES}")
    return value


class Asset:
    kind = "asset"
    input_port_names: tuple[str, ...] = ()
    output_port_names: tuple[str, ...] = ()

    def __init__(self, sim: "Simulation", asset_id: str, name: str, config: dict,
                 position: dict | None = None) -> None:
        self.sim = sim
        self.id = asset_id
        self.name = name
        self.position = dict(position or {"x": 0, "y": 0})
        self.config: dict[str, Any] = self.parse_config(config or {}, sim)
        self.config_version = 1
        self.inputs: dict[str, str] = {}   # input port -> conveyor id
        self.outputs: dict[str, str] = {}  # output port -> conveyor id
        self.state = STOPPED if self.config.get("initial_state") == "stopped" else IDLE
        self.state_reason: str | None = "initial_state" if self.state == STOPPED else None
        self._interval: OperationalInterval | None = None
        self._open_interval()

    # ------------------------------------------------------------------ configuration
    @classmethod
    def parse_config(cls, raw: dict, sim: "Simulation") -> dict:
        raise NotImplementedError

    def check_config_change(self, old: dict, new: dict) -> None:
        """Raise ConflictError when a change cannot be applied in the current state."""

    def effective_boundary(self, changed: set[str]) -> str:
        return "next eligible operation"

    def on_config_changed(self, old: dict, new: dict) -> None:
        self.sim.request_wake(self.id)

    # ------------------------------------------------------------------ ports
    def port_info(self) -> dict:
        return {"inputs": list(self.input_port_names), "outputs": list(self.output_port_names)}

    def validate_input_connection(self, port: str) -> None:
        if port not in self.input_port_names:
            raise ValidationError(f"asset '{self.id}' has no input port '{port}'")
        if port in self.inputs:
            raise ConflictError(f"input port '{port}' of '{self.id}' is already connected")

    def validate_output_connection(self, port: str) -> None:
        if port not in self.output_port_names:
            raise ValidationError(f"asset '{self.id}' has no output port '{port}'")
        if port in self.outputs:
            raise ConflictError(f"output port '{port}' of '{self.id}' is already connected")

    def output_conveyor(self, port: str) -> "Conveyor | None":
        cid = self.outputs.get(port)
        return self.sim.assets.get(cid) if cid else None  # type: ignore[return-value]

    def wake_inputs(self) -> None:
        for cid in self.inputs.values():
            self.sim.request_wake(cid)

    # ------------------------------------------------------------------ behaviour hooks
    def activate(self) -> None:
        """Called when the simulation starts (or when added to a started simulation)."""

    def on_event(self, kind: str, payload: Any) -> None:
        raise NotImplementedError(kind)

    def wake(self) -> None:
        """Re-evaluate: push outputs, start operations, update state."""

    def can_accept(self, port: str, product: Product) -> tuple[bool, str | None]:
        return False, "asset has no inputs"

    def accept(self, port: str, product: Product) -> None:
        raise ConflictError(f"asset '{self.id}' cannot accept products")

    def contained(self) -> Iterable[tuple[str, str]]:
        """Yield (product_id, area) for every active product held by this asset."""
        return ()

    def is_empty(self) -> bool:
        return not any(True for _ in self.contained())

    def is_operational(self) -> bool:
        return self.state not in (STOPPED, FAILED)

    # ------------------------------------------------------------------ helpers
    def push_queue(self, port: str, queue: deque) -> bool:
        """Move products from ``queue`` onto the conveyor attached to ``port`` while it has space."""
        conveyor = self.output_conveyor(port)
        moved = False
        while queue and conveyor is not None and conveyor.has_space():
            product = self.sim.products[queue.popleft()]
            conveyor.admit(product)
            moved = True
        return moved

    def place(self, product: Product, area: str, port: str | None = None,
              status: ProductStatus = ProductStatus.QUEUED) -> None:
        product.location = Location(self.id, area, port)
        product.status = status

    # ------------------------------------------------------------------ commands
    def command(self, command: str) -> None:
        if command == "stop":
            if self.state == STOPPED:
                return
            self.on_stop("stopped by command")
            self.set_state(STOPPED, "command")
        elif command == "fault":
            if self.state == FAILED:
                return
            self.on_stop("fault injected")
            self.set_state(FAILED, "fault injected")
        elif command in ("start", "restart", "clear_fault"):
            if self.state not in (STOPPED, FAILED):
                return
            self.set_state(IDLE, "restarted")
            self.on_restart()
            self.sim.request_wake(self.id)
            self.wake_inputs()
        else:
            raise ValidationError(f"unknown command '{command}' (use start, stop, fault, restart)")

    def on_stop(self, reason: str) -> None:
        """Hook for kinds that must cancel scheduled work when stopped or failing."""

    def on_restart(self) -> None:
        """Hook for kinds that must resume scheduled work."""

    # ------------------------------------------------------------------ state tracking
    def conditions(self) -> dict:
        """Working conditions recorded with operational-history intervals."""
        return {"config_version": self.config_version}

    def _interval_conditions(self) -> dict:
        cond = self.conditions()
        hub = getattr(self.sim, "industrial", None)
        if hub is not None:
            cond.update(hub.interval_conditions(self.id))
        return cond

    def _open_interval(self) -> None:
        self._interval = OperationalInterval(self.id, self.state, self.sim.now, None,
                                             self.state_reason, self._interval_conditions())
        self.sim.operational_history.setdefault(self.id, []).append(self._interval)

    def set_state(self, state: str, reason: str | None = None, *, force_record: bool = False) -> None:
        if state == self.state and reason == self.state_reason and not force_record:
            return
        previous = self.state
        iv = self._interval
        if iv is not None and state == previous and iv.start == self.sim.now and iv.end is None:
            # Same state re-labelled at the same instant: amend instead of recording a 0 s interval.
            self.state_reason = iv.reason = reason
            iv.conditions = self._interval_conditions()
            return
        if self._interval is not None:
            self._interval.end = self.sim.now
        self.state = state
        self.state_reason = reason
        self._open_interval()
        if state == previous:
            return
        if state == STOPPED:
            self.sim.emit("machine_stopped", self, status=state, details={"reason": reason, "previous_state": previous})
        elif state == FAILED:
            self.sim.emit("fault_detected", self, status=state, error=reason, details={"previous_state": previous})
        elif previous in (STOPPED, FAILED):
            self.sim.emit("machine_restarted", self, status=state, details={"previous_state": previous})
        elif state == BLOCKED:
            self.sim.emit("conveyor_blocked" if self.kind == "conveyor" else "asset_blocked", self,
                          status=state, error=reason)
        elif previous == BLOCKED:
            self.sim.emit("asset_unblocked", self, status=state)

    def record_conditions_change(self) -> None:
        """Start a new operational interval because working conditions changed."""
        self.set_state(self.state, self.state_reason, force_record=True)

    # ------------------------------------------------------------------ views
    def counts(self) -> dict:
        return {}

    def stats(self) -> dict:
        return {}

    def to_config_dict(self) -> dict:
        return copy.deepcopy(self.config)

    def snapshot(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "name": self.name, "state": self.state,
            "state_reason": self.state_reason, "position": dict(self.position),
            "config": self.to_config_dict(), "config_version": self.config_version,
            "ports": self.port_info(), "inputs": dict(self.inputs), "outputs": dict(self.outputs),
            "counts": self.counts(),
        }
