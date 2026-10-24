"""FakeClock.on_change (+ unsubscribe); core Clock is now()-only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from coscientist.core.events.clock import FakeClock, SystemClock


def test_fake_clock_on_change_fires_on_advance() -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    seen: list[datetime] = []
    clock.on_change(lambda: seen.append(clock.now()))
    clock.advance(timedelta(seconds=3))
    assert seen == [datetime(2026, 10, 24, 0, 0, 3, tzinfo=UTC)]


def test_fake_clock_on_change_fires_on_set() -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    seen: list[datetime] = []
    clock.on_change(lambda: seen.append(clock.now()))
    clock.set(datetime(2026, 10, 25, tzinfo=UTC))
    assert seen == [datetime(2026, 10, 25, tzinfo=UTC)]


def test_fake_clock_unsubscribe_stops_callbacks() -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    seen: list[datetime] = []
    unsub = clock.on_change(lambda: seen.append(clock.now()))
    unsub()
    clock.advance(timedelta(seconds=1))
    assert seen == []


def test_fake_clock_unsubscribe_is_idempotent() -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    seen: list[int] = []
    unsub = clock.on_change(lambda: seen.append(1))
    unsub()
    unsub()
    clock.advance(timedelta(seconds=1))
    assert seen == []


def test_system_clock_now_is_timezone_aware() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
