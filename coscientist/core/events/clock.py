"""Injectable clock for deadlines and leases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def on_change(self, callback: Callable[[], None]) -> None:
        """Subscribe to clock advances (FakeClock) or accept no-op (wall)."""
        ...


def _require_callable(callback: Callable[[], None]) -> None:
    if not callable(callback):
        raise TypeError(f"on_change callback must be callable, got {callback!r}")


class SystemClock:
    """Wall clock. ``on_change`` accepts subscribers but never fires them —

    waiters bound to deadlines use wall ``timeout`` from ``now()`` deltas.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)

    def on_change(self, callback: Callable[[], None]) -> None:
        _require_callable(callback)


class FakeClock:
    """Controllable clock for property and unit tests.

    ``on_change`` listeners run after ``advance`` / ``set`` so waiters bound to
    deadlines (e.g. JobStore.tick) wake without wall-clock polling.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._on_change: list[Callable[[], None]] = []

    def now(self) -> datetime:
        return self._now

    def on_change(self, callback: Callable[[], None]) -> None:
        _require_callable(callback)
        self._on_change.append(callback)

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        self._fire_change()
        return self._now

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        self._now = when
        self._fire_change()

    def _fire_change(self) -> None:
        for callback in list(self._on_change):
            callback()
