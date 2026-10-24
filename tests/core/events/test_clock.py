"""FakeClock.on_change (+ unsubscribe); core Clock is now()-only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from coscientist.core.events.clock import ClockWithChange, FakeClock, SystemClock


def test_fake_clock_on_change_fires_on_advance_and_set() -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    seen: list[datetime] = []
    unsub = clock.on_change(lambda: seen.append(clock.now()))
    clock.advance(timedelta(seconds=3))
    assert seen == [datetime(2026, 10, 24, 0, 0, 3, tzinfo=UTC)]
    clock.set(datetime(2026, 10, 25, tzinfo=UTC))
    assert seen[-1] == datetime(2026, 10, 25, tzinfo=UTC)
    assert len(seen) == 2
    unsub()
    clock.advance(timedelta(seconds=1))
    assert len(seen) == 2


@pytest.mark.parametrize("bad", [None, 123], ids=["none", "int"])
def test_fake_clock_on_change_refuses_non_callable(bad: object) -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    callback: Any = bad
    with pytest.raises(TypeError, match="callable"):
        clock.on_change(callback)


def test_system_clock_is_not_clock_with_change() -> None:
    assert not isinstance(SystemClock(), ClockWithChange)
    assert isinstance(FakeClock(datetime(2026, 10, 24, tzinfo=UTC)), ClockWithChange)


def test_fake_clock_unsubscribe_is_idempotent() -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    seen: list[int] = []
    unsub = clock.on_change(lambda: seen.append(1))
    unsub()
    unsub()
    clock.advance(timedelta(seconds=1))
    assert seen == []
