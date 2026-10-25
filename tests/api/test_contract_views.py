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
    StudyViewLoopRefuse,
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
from coscientist.core.events.errors import STUDY_NOT_FOUND_REASON, StudyNotFound
from coscientist.core.events.events import EventType, StoredEvent, UnstampedEvent
from coscientist.core.events.ids import (
    is_study_id,
    study_id,
    study_id_from_idempotency_key,
)
from coscientist.core.events.ports import EventStore, StudyService
from coscientist.core.execution.domain import StudyView
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import RC3T_THRESHOLDS

# Well-formed fakes (same shape as core generators; not path-like).
_SID = "st_" + "a" * 27
_SID_OTHER = "st_" + "b" * 27
_SID_FOREIGN = "st_" + "c" * 27
_SID_MISSING = "st_" + "d" * 27


class _FakeStudyService:
    """StudyService double: known ids → view; unknown → StudyNotFound."""

    def __init__(self, views: dict[str, StudyView]) -> None:
        self._views = views

    def study_view(self, study_id: str) -> StudyView:
        if study_id not in self._views:
            raise StudyNotFound(study_id, reason=STUDY_NOT_FOUND_REASON)
        return self._views[study_id]


class _FakeEventStore:
    """EventStore double for ``list_study_events`` (full Protocol surface)."""

    study_id: str
    owner: str | None
    epoch: int
    owner_nonce: str | None

    def __init__(self, study_id: str, events: list[StoredEvent]) -> None:
        self.study_id = study_id
        self.owner = None
        self.epoch = 0
        self.owner_nonce = None
        self._events = events

    async def append(self, event: UnstampedEvent) -> StoredEvent:
        raise NotImplementedError("append")

    async def append_many(self, events: list[UnstampedEvent]) -> list[StoredEvent]:
        raise NotImplementedError("append_many")

    async def read_all(self) -> list[StoredEvent]:
        raise NotImplementedError("read_all")

    async def read_after(self, seq: int) -> list[StoredEvent]:
        return [e for e in self._events if e.seq is not None and e.seq > seq]

    async def get_projection(self, table: str) -> list[dict[str, Any]]:
        raise NotImplementedError("get_projection")

    async def get_row(self, table: str, key: str) -> dict[str, Any] | None:
        raise NotImplementedError("get_row")

    async def upsert_projection(self, table: str, key: str, data: dict[str, Any]) -> None:
        raise NotImplementedError("upsert_projection")

    async def acquire_lease(self, owner: str, ttl_s: float = 30.0) -> int:
        raise NotImplementedError("acquire_lease")

    async def renew_lease(
        self,
        owner: str,
        epoch: int,
        ttl_s: float = 30.0,
        *,
        nonce: str | None = None,
    ) -> None:
        raise NotImplementedError("renew_lease")

    async def release_lease(
        self,
        owner: str,
        epoch: int,
        *,
        nonce: str | None = None,
    ) -> None:
        raise NotImplementedError("release_lease")

    async def current_lease(self) -> tuple[str | None, int, datetime | None]:
        raise NotImplementedError("current_lease")

    async def close(self) -> None:
        return None


def _ts() -> datetime:
    return datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)


def _stored(
    *,
    seq: int,
    typ: EventType = "StudyCreated",
    study_id: str = _SID,
    payload: dict[str, Any],
) -> StoredEvent:
    return StoredEvent.model_validate(
        {
            "seq": seq,
            "study_id": study_id,
            "type": typ,
            "entity_key": study_id,
            "idem_key": f"{typ}:{study_id}",
            "payload": payload,
            "ts": _ts(),
            "schema_version": 1,
            "epoch": 1,
            "payload_digest": "a" * 64,
        }
    )


def _row(
    seq: int,
    *,
    typ: EventType = "StudyCreated",
    study_id: str = _SID,
) -> StoredEvent:
    return _stored(seq=seq, typ=typ, study_id=study_id, payload={"study_id": study_id})


def test_require_study_id_accepts_study_id_generator() -> None:
    sid = study_id()
    assert is_study_id(sid)
    assert require_study_id(sid) == sid


def test_require_study_id_accepts_idempotency_generator() -> None:
    idem = study_id_from_idempotency_key("contract-key")
    assert is_study_id(idem)
    assert require_study_id(idem) == idem


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        (None, r"study_id must be str"),
        (1, r"study_id must be str"),
        (True, r"study_id must be str"),
        ("", "study_id_empty_or_whitespace"),
        ("  ", "study_id_empty_or_whitespace"),
        (" st", "study_id_empty_or_whitespace"),
        ("st ", "study_id_empty_or_whitespace"),
        ("\tst", "study_id_empty_or_whitespace"),
        ("../x", "study_id_format"),
        ("/abs", "study_id_format"),
        ("a/b", "study_id_format"),
        ("st_foo/bar", "study_id_format"),
        ("st_foo\\bar", "study_id_format"),
        ("not_st_prefix", "study_id_format"),
        ("st_abc", "study_id_format"),
        ("st_" + "a" * 26, "study_id_format"),
        ("st_idem_" + "a" * 23, "study_id_format"),
    ],
    ids=[
        "none",
        "int",
        "bool",
        "empty",
        "spaces",
        "leading_space",
        "trailing_space",
        "tab",
        "dotdot",
        "abs",
        "slash",
        "st_slash",
        "st_backslash",
        "no_prefix",
        "short_st",
        "short_hex",
        "short_idem",
    ],
)
def test_require_study_id_refuses(bad: object, match: str) -> None:
    with pytest.raises(StudyIdRefuse, match=match):
        require_study_id(bad)


@pytest.mark.parametrize("after", [0, 3], ids=["zero", "three"])
def test_require_after_seq_accepts(after: int) -> None:
    assert require_after_seq(after) == after


@pytest.mark.parametrize(
    "bad",
    [None, "0", 0.0, True, False, -1, -10],
    ids=["none", "str", "float", "true", "false", "neg1", "neg10"],
)
def test_require_after_seq_refuses(bad: object) -> None:
    with pytest.raises(AfterSeqRefuse, match="after_must_be_"):
        require_after_seq(bad)


def test_get_study_returns_view() -> None:
    view = StudyView(phase="running", progress=0.25, awaiting_human=False)
    svc: StudyService = _FakeStudyService({_SID: view})
    got = get_study(svc, _SID)
    assert got == view
    assert got.phase == "running"
    assert got.progress == 0.25


def test_get_study_not_found() -> None:
    svc: StudyService = _FakeStudyService({})
    with pytest.raises(StudyNotFound, match=STUDY_NOT_FOUND_REASON) as ei:
        get_study(svc, _SID_MISSING)
    assert ei.value.study_id == _SID_MISSING


def test_get_study_refuses_bad_id_before_service() -> None:
    class _Boom(_FakeStudyService):
        def study_view(self, study_id: str) -> StudyView:
            raise AssertionError("service must not be called")

    with pytest.raises(StudyIdRefuse, match="study_id_empty_or_whitespace"):
        get_study(_Boom({}), "")


def _create_study(tmp_path: Path) -> str:
    catalog = minimal_test_catalog()

    async def _create() -> str:
        out = await post_studies(
            tmp_path,
            CreateStudyBody(
                request="real svc",
                profile="research",
                catalog_version=3,
                require_hypotheses=False,
                **RC3T_THRESHOLDS,
            ),
            catalog=catalog,
        )
        return out["study_id"]

    return asyncio.run(_create())


def test_data_root_study_service_view(tmp_path: Path) -> None:
    sid = _create_study(tmp_path)
    view = DataRootStudyService(tmp_path, catalog=minimal_test_catalog()).study_view(sid)
    assert view.phase == "running"


def test_data_root_study_service_not_found(tmp_path: Path) -> None:
    svc = DataRootStudyService(tmp_path, catalog=minimal_test_catalog())
    with pytest.raises(StudyNotFound, match=STUDY_NOT_FOUND_REASON) as ei:
        svc.study_view(_SID_MISSING)
    assert ei.value.study_id == _SID_MISSING


def test_data_root_study_service_get_study_matches_view(tmp_path: Path) -> None:
    sid = _create_study(tmp_path)
    svc = DataRootStudyService(tmp_path, catalog=minimal_test_catalog())
    assert get_study(svc, sid) == svc.study_view(sid)


@pytest.mark.parametrize(
    "bad",
    ["../x", "/abs", "a/b"],
    ids=["dotdot", "abs", "slash"],
)
def test_data_root_study_service_path_traversal(tmp_path: Path, bad: str) -> None:
    svc = DataRootStudyService(tmp_path, catalog=minimal_test_catalog())
    with pytest.raises(StudyIdRefuse, match="study_id_format"):
        svc.study_view(bad)


def test_data_root_study_service_missing_does_not_mkdir(tmp_path: Path) -> None:
    svc = DataRootStudyService(tmp_path, catalog=minimal_test_catalog())
    before = set(tmp_path.rglob("*"))
    with pytest.raises(StudyNotFound, match=STUDY_NOT_FOUND_REASON):
        svc.study_view(_SID_MISSING)
    assert set(tmp_path.rglob("*")) == before
    assert not (tmp_path / _SID_MISSING).exists()


def test_data_root_study_service_refuses_under_running_loop(tmp_path: Path) -> None:
    svc = DataRootStudyService(tmp_path, catalog=minimal_test_catalog())

    async def _under_loop() -> None:
        with pytest.raises(StudyViewLoopRefuse, match="study_view_requires_no_running_event_loop"):
            svc.study_view(_SID_MISSING)

    asyncio.run(_under_loop())


@pytest.mark.parametrize(
    ("after", "want"),
    [(0, [1, 2, 3]), (1, [2, 3]), (3, [])],
    ids=["all", "mid", "empty"],
)
@pytest.mark.asyncio
async def test_list_study_events_after_filters(after: int, want: list[int]) -> None:
    store: EventStore = _FakeEventStore(_SID, [_row(1), _row(2, typ="TaskCreated"), _row(3)])
    rows = await list_study_events(store, study_id=_SID, after=after)
    assert [e.seq for e in rows] == want


@pytest.mark.parametrize(
    "bad",
    [True, -1],
    ids=["bool", "neg"],
)
@pytest.mark.asyncio
async def test_list_study_events_refuses_bad_after(bad: object) -> None:
    store: EventStore = _FakeEventStore(_SID, [])
    with pytest.raises(AfterSeqRefuse, match="after_must_be_"):
        await list_study_events(store, study_id=_SID, after=bad)


@pytest.mark.asyncio
async def test_list_study_events_refuses_foreign_store() -> None:
    store: EventStore = _FakeEventStore(_SID_OTHER, [_row(1, study_id=_SID_OTHER)])
    with pytest.raises(StudyStoreMismatch, match="store_study_id_mismatch"):
        await list_study_events(store, study_id=_SID, after=0)


@pytest.mark.asyncio
async def test_list_study_events_refuses_event_study_id_mismatch() -> None:
    store: EventStore = _FakeEventStore(_SID, [_row(1, study_id=_SID_FOREIGN)])
    with pytest.raises(StudyStoreMismatch, match="event_study_id_mismatch"):
        await list_study_events(store, study_id=_SID, after=0)


def test_event_to_wire_round_trip_fields() -> None:
    row = _stored(
        seq=7,
        typ="StudyCancelled",
        payload={"study_id": _SID, "by": "user"},
    )
    wire = event_to_wire(row)
    assert wire.seq == 7
    assert wire.type == "StudyCancelled"
    assert wire.payload == {"study_id": _SID, "by": "user"}
    assert wire.payload_digest == "a" * 64
    dumped = wire.model_dump(mode="json")
    assert dumped["seq"] == 7
    assert dumped["ts"] == "2026-10-24T12:00:00Z"


def test_event_to_wire_payload_is_copy() -> None:
    payload = {"study_id": _SID, "n": 1}
    row = _stored(seq=1, payload=payload)
    wire = event_to_wire(row)
    wire.payload["n"] = 99
    assert row.payload["n"] == 1
    assert payload["n"] == 1


def test_event_to_wire_refuses_missing_seq() -> None:
    row = _row(1)
    bad = row.model_copy(update={"seq": None})
    with pytest.raises(EventWireRefuse, match="event_wire_validate"):
        event_to_wire(bad)


def test_event_to_wire_refuses_non_positive_seq() -> None:
    with pytest.raises(EventWireRefuse, match="event_wire_validate"):
        event_to_wire(_row(1).model_copy(update={"seq": 0}))


def test_event_to_wire_refuses_empty_digest() -> None:
    with pytest.raises(EventWireRefuse, match="event_wire_validate"):
        event_to_wire(_row(1).model_copy(update={"payload_digest": ""}))


def test_event_wire_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra_field"):
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
