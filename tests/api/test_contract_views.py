"""M5 API contract: GET /studies/{id} + GET /studies/{id}/events (ports + fakes)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.api.errors import (
    AfterSeqRefuse,
    EventWireRefuse,
    StudyIdRefuse,
    StudyStoreMismatch,
)
from coscientist.api.studies import CreateStudyBody, post_studies
from coscientist.api.study_service import DataRootStudyService
from coscientist.api.views import (
    EventWire,
    event_to_wire,
    get_study,
    list_study_events,
    require_after_seq,
    require_study_id,
)
from coscientist.core.events.errors import StudyNotFound
from coscientist.core.events.events import EventType, StoredEvent
from coscientist.core.events.ports import StudyService
from coscientist.core.execution.domain import StudyView
from tests.checks.minimal_test_catalog import minimal_test_catalog


class _FakeStudyService:
    """StudyService double: known ids → view; unknown → StudyNotFound."""

    def __init__(self, views: dict[str, StudyView]) -> None:
        self._views = views

    def study_view(self, study_id: str) -> StudyView:
        if study_id not in self._views:
            raise StudyNotFound(study_id)
        return self._views[study_id]


class _FakeEventStore:
    """EventStore double exposing ``read_after`` + ``study_id``."""

    owner = None
    epoch = 0
    owner_nonce = None

    def __init__(self, study_id: str, events: list[StoredEvent]) -> None:
        self.study_id = study_id
        self._events = events

    async def read_after(self, seq: int) -> list[StoredEvent]:
        return [e for e in self._events if e.seq is not None and e.seq > seq]


def _ts() -> datetime:
    return datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)


def _stored(
    *,
    seq: int,
    typ: EventType = "StudyCreated",
    study_id: str = "st_fake",
    payload: dict[str, Any] | None = None,
) -> StoredEvent:
    return StoredEvent.model_validate(
        {
            "seq": seq,
            "study_id": study_id,
            "type": typ,
            "entity_key": study_id,
            "idem_key": f"{typ}:{study_id}",
            "payload": payload if payload is not None else {"study_id": study_id},
            "ts": _ts(),
            "schema_version": 1,
            "epoch": 1,
            "payload_digest": "a" * 64,
        }
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
    svc: StudyService = _FakeStudyService({"st_1": view})
    got = get_study(svc, "st_1")
    assert got == view
    assert got.phase == "running"
    assert got.progress == 0.25


def test_get_study_not_found() -> None:
    svc: StudyService = _FakeStudyService({})
    with pytest.raises(StudyNotFound) as ei:
        get_study(svc, "st_missing")
    assert ei.value.study_id == "st_missing"


def test_get_study_refuses_bad_id_before_service() -> None:
    class _Boom(_FakeStudyService):
        def study_view(self, study_id: str) -> StudyView:
            raise AssertionError("service must not be called")

    with pytest.raises(StudyIdRefuse):
        get_study(_Boom({}), "")


def test_data_root_study_service_not_found_and_view(tmp_path: Path) -> None:
    catalog = minimal_test_catalog()

    async def _create() -> str:
        out = await post_studies(
            tmp_path,
            CreateStudyBody(
                request="real svc",
                profile="research",
                catalog_version=3,
                require_hypotheses=False,
            ),
            catalog=catalog,
        )
        return out["study_id"]

    sid = asyncio.run(_create())
    svc = DataRootStudyService(tmp_path, catalog=catalog)
    view = svc.study_view(sid)
    assert view.phase in ("running", "finalizing", "closed")
    with pytest.raises(StudyNotFound) as ei:
        svc.study_view("st_missing")
    assert ei.value.study_id == "st_missing"
    assert get_study(svc, sid) == view


@pytest.mark.asyncio
async def test_list_study_events_after_filters() -> None:
    store = _FakeEventStore(
        "st_fake",
        [_stored(seq=1), _stored(seq=2, typ="TaskCreated"), _stored(seq=3)],
    )
    all_rows = await list_study_events(store, study_id="st_fake", after=0)
    assert [e.seq for e in all_rows] == [1, 2, 3]
    mid = await list_study_events(store, study_id="st_fake", after=1)
    assert [e.seq for e in mid] == [2, 3]
    empty = await list_study_events(store, study_id="st_fake", after=3)
    assert empty == []


@pytest.mark.asyncio
async def test_list_study_events_refuses_bad_after() -> None:
    store = _FakeEventStore("st_fake", [])
    with pytest.raises(AfterSeqRefuse):
        await list_study_events(store, study_id="st_fake", after=True)
    with pytest.raises(AfterSeqRefuse):
        await list_study_events(store, study_id="st_fake", after=-1)


@pytest.mark.asyncio
async def test_list_study_events_refuses_foreign_store() -> None:
    store = _FakeEventStore("st_other", [_stored(seq=1, study_id="st_other")])
    with pytest.raises(StudyStoreMismatch):
        await list_study_events(store, study_id="st_fake", after=0)


@pytest.mark.asyncio
async def test_list_study_events_refuses_event_study_id_mismatch() -> None:
    store = _FakeEventStore("st_fake", [_stored(seq=1, study_id="st_foreign")])
    with pytest.raises(StudyStoreMismatch):
        await list_study_events(store, study_id="st_fake", after=0)


def test_event_to_wire_round_trip_fields() -> None:
    row = _stored(
        seq=7, typ="StudyCancelled", payload={"study_id": "st_fake", "by": "user"}
    )
    wire = event_to_wire(row)
    assert isinstance(wire, EventWire)
    assert wire.seq == 7
    assert wire.type == "StudyCancelled"
    assert wire.payload == {"study_id": "st_fake", "by": "user"}
    assert wire.payload_digest == "a" * 64
    dumped = wire.model_dump(mode="json")
    assert dumped["seq"] == 7
    assert "ts" in dumped


def test_event_to_wire_payload_is_copy() -> None:
    payload = {"study_id": "st_fake", "n": 1}
    row = _stored(seq=1, payload=payload)
    wire = event_to_wire(row)
    wire.payload["n"] = 99
    assert row.payload["n"] == 1
    assert payload["n"] == 1


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
        EventWire.model_validate(
            {
                "seq": 1,
                "study_id": "st",
                "type": "StudyCreated",
                "entity_key": "st",
                "idem_key": "x",
                "payload": {},
                "ts": _ts(),
                "schema_version": 1,
                "epoch": 1,
                "payload_digest": "a" * 64,
                "extra_field": True,
            }
        )
