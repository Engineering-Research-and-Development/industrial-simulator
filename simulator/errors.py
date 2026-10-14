"""Error types shared by the engine and the API layer."""


class SimulatorError(Exception):
    """Base class for all simulator errors."""

    status_code = 400


class ValidationError(SimulatorError):
    """Invalid configuration or request payload."""

    status_code = 422


class NotFoundError(SimulatorError):
    """Unknown identifier."""

    status_code = 404


class ConflictError(SimulatorError):
    """Operation not possible in the current state (e.g. would lose products)."""

    status_code = 409
