"""Clock protocol: FakeClock.on_change + SystemClock validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coscientist.core.events.clock import FakeClock, SystemClock


def test_fake_clock_on_change_fires_on_advance_and_set() -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    seen: list[datetime] = []
    clock.on_change(lambda: seen.append(clock.now()))
    clock.advance(timedelta(seconds=3))
    assert seen == [datetime(2026, 10, 24, 0, 0, 3, tzinfo=UTC)]
    clock.set(datetime(2026, 10, 25, tzinfo=UTC))
    assert seen[-1] == datetime(2026, 10, 25, tzinfo=UTC)
    assert len(seen) == 2


@pytest.mark.parametrize("clock_cls", [FakeClock, SystemClock])
def test_on_change_refuses_non_callable(clock_cls: type) -> None:
    clock = clock_cls() if clock_cls is SystemClock else FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    with pytest.raises(TypeError, match="callable"):
        clock.on_change(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="callable"):
        clock.on_change(123)  # type: ignore[arg-type]


def test_system_clock_on_change_accepts_callable_without_firing() -> None:
    clock = SystemClock()
    fired = False

    def _cb() -> None:
        nonlocal fired
        fired = True

    clock.on_change(_cb)
    assert fired is False
    assert clock.now().tzinfo is not None
