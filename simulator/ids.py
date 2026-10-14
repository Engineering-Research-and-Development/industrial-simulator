"""Collision-safe identifier generation.

Identifiers are UUID4 values. When the simulation is seeded, UUIDs are drawn from a
dedicated seeded RNG so that runs are reproducible; otherwise OS randomness is used.
The identifier RNG is independent from the defect RNG so that changing how many IDs are
generated never shifts the defect random stream.
"""

from __future__ import annotations

import random
import re
import uuid

from .errors import ValidationError

ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")


def validate_id(value: object, what: str = "id") -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise ValidationError(
            f"invalid {what} {value!r}: must be 1-64 chars of letters, digits, '_', '.', ':' or '-'"
        )
    return value


class IdFactory:
    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(f"ids-{seed}") if seed is not None else None
        self._issued: set[str] = set()

    def new(self, prefix: str) -> str:
        while True:
            if self._rng is None:
                raw = uuid.uuid4()
            else:
                raw = uuid.UUID(int=self._rng.getrandbits(128), version=4)
            value = f"{prefix}-{raw}"
            if value not in self._issued:
                self._issued.add(value)
                return value

    def reserve(self, value: str) -> None:
        """Mark an externally supplied ID as used so generated IDs never collide with it."""
        self._issued.add(value)
