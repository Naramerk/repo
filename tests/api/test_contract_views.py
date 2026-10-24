"""M5 API contract: GET /studies/{id} + GET /studies/{id}/events (ports + fakes)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.api.errors import (
    AfterSeqRefuse,
    EventWireRefuse,
    StudyIdRefuse,
    StudyNotFound,
)
from coscientist.api.views import (
    EventWire,
    event_to_wire,
    get_study,
    list_study_events,
    require_after_seq,
    require_study_id,
)
from coscientist.core.events.events import StoredEvent
from coscientist.core.execution.domain import StudyView


class _FakeStudyService:
    """StudyService double: known ids → view; unknown → StudyNotFound."""

    def __init__(self, views: dict[str, StudyView]) -> None:
        self._views = views

    def study_view(self, study_id: str) -> StudyView:
        if study_id not in self._views:
            raise StudyNotFound(study_id)
        return self._views[study_id]


class _FakeEventStore:
    """EventStore double exposing only ``read_after`` (list_study_events contract)."""

    study_id = "st_fake"
    owner = None
    epoch = 0
    owner_nonce = None

    def __init__(self, events: list[StoredEvent]) -> None:
        self._events = events

    async def read_after(self, seq: int) -> list[StoredEvent]:
        return [e for e in self._events if e.seq is not None and e.seq > seq]


def _ts() -> datetime:
    return datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)


def _stored(
    *,
    seq: int,
    typ: str = "StudyCreated",
    payload: dict[str, Any] | None = None,
) -> StoredEvent:
    return StoredEvent(
        seq=seq,
        study_id="st_fake",
        type=typ,  # type: ignore[arg-type]
        entity_key="st_fake",
        idem_key=f"{typ}:st_fake",
        payload=payload if payload is not None else {"study_id": "st_fake"},
        ts=_ts(),
        schema_version=1,
        epoch=1,
        payload_digest="a" * 64,
    )


def test_require_study_id_accepts_plain() -> None:
    assert require_study_id("st_abc") == "st_abc"


@pytest.mark.parametrize(
    "bad",
    [None, 1, True, "", "  ", " st", "st ", "\tst"],
)
def test_require_study_id_refuses(bad: object) -> None:
    with pytest.raises(StudyIdRefuse):
        require_study_id(bad)


def test_require_after_seq_accepts_zero_and_positive() -> None:
    assert require_after_seq(0) == 0
    assert require_after_seq(3) == 3


@pytest.mark.parametrize(
    "bad",
    [None, "0", 0.0, True, False, -1, -10],
)
def test_require_after_seq_refuses(bad: object) -> None:
    with pytest.raises(AfterSeqRefuse):
        require_after_seq(bad)


def test_get_study_returns_view() -> None:
    view = StudyView(phase="running", progress=0.25, awaiting_human=False)
    svc = _FakeStudyService({"st_1": view})
    got = get_study(svc, "st_1")
    assert got == view
    assert got.phase == "running"
    assert got.progress == 0.25


def test_get_study_not_found() -> None:
    svc = _FakeStudyService({})
    with pytest.raises(StudyNotFound) as ei:
        get_study(svc, "st_missing")
    assert ei.value.study_id == "st_missing"


def test_get_study_refuses_bad_id_before_service() -> None:
    class _Boom:
        def study_view(self, study_id: str) -> StudyView:
            raise AssertionError("service must not be called")

    with pytest.raises(StudyIdRefuse):
        get_study(_Boom(), "")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_list_study_events_after_filters() -> None:
    store = _FakeEventStore([_stored(seq=1), _stored(seq=2, typ="TaskCreated"), _stored(seq=3)])
    all_rows = await list_study_events(store, after=0)
    assert [e.seq for e in all_rows] == [1, 2, 3]
    mid = await list_study_events(store, after=1)
    assert [e.seq for e in mid] == [2, 3]
    empty = await list_study_events(store, after=3)
    assert empty == []


@pytest.mark.asyncio
async def test_list_study_events_refuses_bad_after() -> None:
    store = _FakeEventStore([])
    with pytest.raises(AfterSeqRefuse):
        await list_study_events(store, after=True)
    with pytest.raises(AfterSeqRefuse):
        await list_study_events(store, after=-1)


def test_event_to_wire_round_trip_fields() -> None:
    row = _stored(seq=7, typ="StudyCancelled", payload={"study_id": "st_fake", "by": "user"})
    wire = event_to_wire(row)
    assert isinstance(wire, EventWire)
    assert wire.seq == 7
    assert wire.type == "StudyCancelled"
    assert wire.payload == {"study_id": "st_fake", "by": "user"}
    assert wire.payload_digest == "a" * 64
    dumped = wire.model_dump(mode="json")
    assert dumped["seq"] == 7
    assert "ts" in dumped


def test_event_to_wire_refuses_missing_seq() -> None:
    row = _stored(seq=1)
    bad = row.model_copy(update={"seq": None})
    with pytest.raises(EventWireRefuse):
        event_to_wire(bad)


def test_event_to_wire_refuses_non_positive_seq() -> None:
    row = _stored(seq=1)
    with pytest.raises(EventWireRefuse):
        event_to_wire(row.model_copy(update={"seq": 0}))


def test_event_to_wire_refuses_empty_digest() -> None:
    row = _stored(seq=1)
    with pytest.raises(EventWireRefuse):
        event_to_wire(row.model_copy(update={"payload_digest": ""}))


def test_event_wire_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        EventWire(
            seq=1,
            study_id="st",
            type="StudyCreated",
            entity_key="st",
            idem_key="x",
            payload={},
            ts=_ts(),
            schema_version=1,
            epoch=1,
            payload_digest="a" * 64,
            extra_field=True,  # type: ignore[call-arg]
        )
