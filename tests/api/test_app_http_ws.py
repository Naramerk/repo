"""M5 API: FastAPI routes + WS stream (fakes; extra ``api``)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from coscientist.api.app import AppDeps, create_app
from coscientist.api.errors import StreamConfigRefuse
from coscientist.api.stream import require_poll_interval_s
from coscientist.core.events.errors import StudyNotFound
from coscientist.core.events.events import StoredEvent
from coscientist.core.execution.domain import StudyView

_SID = "st_" + "a" * 27
_SID_MISSING = "st_" + "b" * 27


class _Studies:
    def study_view(self, study_id: str) -> StudyView:
        if study_id != _SID:
            raise StudyNotFound(study_id, reason="study_not_found")
        return StudyView(phase="running")


class _Store:
    study_id = _SID
    owner = None
    epoch = 0
    owner_nonce = None

    def __init__(self, events: list[StoredEvent]) -> None:
        self._events = events

    async def read_after(self, seq: int) -> list[StoredEvent]:
        return [e for e in self._events if e.seq is not None and e.seq > seq]


def _ev(seq: int) -> StoredEvent:
    return StoredEvent(
        seq=seq,
        study_id=_SID,
        type="StudyCreated",
        entity_key=_SID,
        idem_key=f"StudyCreated:{_SID}:{seq}",
        payload={"study_id": _SID},
        ts=datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC),
        schema_version=1,
        epoch=1,
        payload_digest="b" * 64,
    )


def _make_app(
    events: list[StoredEvent],
    calls: list[tuple[str, str]],
) -> TestClient:
    store = _Store(events)

    async def _cancel(sid: str, by: str) -> dict[str, str]:
        calls.append((sid, by))
        return {"study_id": sid}

    def _store_for(sid: str) -> _Store:
        if sid != _SID:
            raise StudyNotFound(sid, reason="study_not_found")
        return store

    return TestClient(
        create_app(
            AppDeps(
                studies=_Studies(),
                event_store_for=_store_for,
                cancel_study=_cancel,
                stream_poll_interval_s=0.01,
            )
        )
    )


def test_poll_interval_refuse() -> None:
    with pytest.raises(StreamConfigRefuse):
        require_poll_interval_s(0)
    with pytest.raises(StreamConfigRefuse):
        require_poll_interval_s(-1.0)


def test_http_and_ws() -> None:
    calls: list[tuple[str, str]] = []
    client = _make_app([_ev(1), _ev(2)], calls)
    assert client.get(f"/studies/{_SID}").json()["phase"] == "running"
    assert client.get(f"/studies/{_SID_MISSING}").status_code == 404
    # malformed path id → 400 via StudyIdRefuse
    assert client.get("/studies/st_no").status_code == 400
    ev = client.get(f"/studies/{_SID}/events", params={"after": 0})
    assert ev.status_code == 200
    assert [row["seq"] for row in ev.json()] == [1, 2]
    assert client.post(f"/studies/{_SID}/cancel", json={"by": "ops"}).json() == {
        "study_id": _SID
    }
    assert calls == [(_SID, "ops")]
    with client.websocket_connect(f"/studies/{_SID}/stream?after=0") as ws:
        assert ws.receive_json()["seq"] == 1
        assert ws.receive_json()["seq"] == 2
