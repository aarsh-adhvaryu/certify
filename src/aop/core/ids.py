"""Injectable clock and id sources.

Every timestamp and identifier in the system comes from one of these, never from
a bare ``datetime.now()`` or ``uuid4()`` inline. Two things depend on it:

* Slot 07 requires the journal to rebuild byte-stable from a fixture. That is
  impossible if construction reaches for the wall clock.
* Replay (Slot 14) has to reproduce a recorded run exactly.

Retrofitting this later would mean touching every schema, so it lands first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Source of the current time."""

    def now(self) -> datetime:  # pragma: no cover - protocol
        ...


class IdSource(Protocol):
    """Source of unique identifiers."""

    def new_id(self, prefix: str) -> str:  # pragma: no cover - protocol
        ...


class SystemClock:
    """Wall clock, always timezone-aware UTC.

    Naive datetimes are banned throughout: they compare and serialize
    inconsistently, and the journal has to round-trip exactly.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Deterministic clock for tests.

    Returns ``start``, then advances by ``step`` on every call, so a sequence of
    events gets distinct but predictable timestamps.
    """

    def __init__(self, start: datetime, step: timedelta = timedelta(seconds=1)) -> None:
        if start.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware start")
        self._current = start
        self._step = step

    def now(self) -> datetime:
        value = self._current
        self._current = self._current + self._step
        return value

    def peek(self) -> datetime:
        """Current time without advancing."""
        return self._current


class UuidIds:
    """Random identifiers: ``<prefix>_<32 hex chars>``."""

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"


class SequentialIds:
    """Deterministic identifiers for tests: ``<prefix>_0001``, ``<prefix>_0002``.

    Counters are per-prefix, so interleaving tasks and attempts stays readable.
    """

    def __init__(self, width: int = 4) -> None:
        self._counters: dict[str, int] = {}
        self._width = width

    def new_id(self, prefix: str) -> str:
        nxt = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = nxt
        return f"{prefix}_{nxt:0{self._width}d}"
