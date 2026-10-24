"""Production schema collapse to v1: empty upcaster chains, no literals, helper."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import (
    EventType,
    UnstampedEvent,
    make_unstamped_event,
)
from coscientist.core.events.schema_gate import (
    require_writer_schema_version,
    upcast_strict,
)
from coscientist.core.events.upcasters import (
    UPCASTERS,
    assert_upcaster_chain_continuous,
    register_upcaster,
    upcast,
    writer_schema_version,
)

ROOT = Path(__file__).resolve().parents[1]

def _clear_production_upcasters() -> None:
    """Tests must not leave production-type registrations; clear before asserts."""
    from typing import get_args

    from coscientist.core.events.events import EventType

    for et in get_args(EventType):
        UPCASTERS.pop(et, None)


COSCIENTIST = ROOT / "coscientist"
_SCHEMA_VERSION_LITERAL = re.compile(r"schema_version\s*=\s*\d")


def test_all_known_event_types_are_writer_v1_with_empty_upcasters() -> None:
    _clear_production_upcasters()
    for et in get_args(EventType):
        assert writer_schema_version(et) == 1, et
        bucket = UPCASTERS.get(et, {})
        assert bucket == {}, f"{et}: expected empty upcaster bucket, got {bucket!r}"


def test_no_schema_version_digit_literals_in_coscientist_tree() -> None:
    hits: list[str] = []
    for path in COSCIENTIST.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if _SCHEMA_VERSION_LITERAL.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{i}:{line.strip()}")
    assert hits == [], "schema_version digit literals remain:\n" + "\n".join(hits)


def test_event_envelope_alias_is_gone() -> None:
    import coscientist.core.events.events as events_mod

    assert not hasattr(events_mod, "EventEnvelope")
    assert "EventEnvelope" not in events_mod.__dict__


def test_make_unstamped_event_stamps_writer_version_from_registry() -> None:
    _clear_production_upcasters()
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    ev = make_unstamped_event(
        study_id="st1",
        type="StudyCreated",
        entity_key="st1",
        idem_key="StudyCreated:st1",
        payload={"study_id": "st1", "catalog_version": 1},
        ts=clock.now(),
    )
    assert isinstance(ev, UnstampedEvent)
    assert ev.schema_version == require_writer_schema_version("StudyCreated") == 1


def test_synthetic_upcaster_chain_continuous_and_refuses_gaps() -> None:
    fake = "_SyntheticCollapseType"
    UPCASTERS.pop(fake, None)
    try:
        register_upcaster(fake, 1, lambda p: {**p, "v": 2})
        register_upcaster(fake, 2, lambda p: {**p, "v": 3})
        assert_upcaster_chain_continuous(fake, 3)
        assert writer_schema_version(fake) == 3
        v, data = upcast(fake, 1, {"x": 1})
        assert v == 3 and data["v"] == 3

        # Gap: remove from_version=2 → continuous assert fails.
        del UPCASTERS[fake][2]
        with pytest.raises(AssertionError, match="gaps"):
            assert_upcaster_chain_continuous(fake, 3)

        # Restore chain; version ahead of writer refused via upcast_strict.
        register_upcaster(fake, 2, lambda p: {**p, "v": 3})
        with pytest.raises(JournalFormatError, match="ahead of writer"):
            upcast_strict(fake, writer_schema_version(fake) + 1, {})
    finally:
        UPCASTERS.pop(fake, None)


def test_upcast_strict_still_refuses_unknown_and_ahead() -> None:
    with pytest.raises(JournalFormatError, match="unknown event type"):
        upcast_strict("NotAnEventTypeEver", 1, {})
    with pytest.raises(JournalFormatError, match="ahead of writer"):
        upcast_strict("StudyCreated", writer_schema_version("StudyCreated") + 1, {})
