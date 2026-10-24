"""M5 API: FastAPI routes + WS stream (fakes; extra ``api``)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from coscientist.api.app import AppDeps, create_app
from coscientist.api.errors import StreamConfigRefuse, StudyNotFound
from coscientist.api.stream import require_poll_interval_s
from coscientist.core.events.events import StoredEvent
from coscientist.core.execution.domain import StudyView


class _Studies:
    def study_view(self, study_id: str) -> StudyView:
        if study_id != "st_1":
            raise StudyNotFound(study_id)
        return StudyView(phase="running")


class _Store:
    study_id = "st_1"
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
        study_id="st_1",
        type="StudyCreated",
        entity_key="st_1",
        idem_key=f"StudyCreated:st_1:{seq}",
        payload={"study_id": "st_1"},
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
        if sid != "st_1":
            raise StudyNotFound(sid)
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
    assert client.get("/studies/st_1").json()["phase"] == "running"
    assert client.get("/studies/st_no").status_code == 404
    ev = client.get("/studies/st_1/events", params={"after": 0})
    assert ev.status_code == 200
    assert [row["seq"] for row in ev.json()] == [1, 2]
    assert client.post("/studies/st_1/cancel", json={"by": "ops"}).json() == {
        "study_id": "st_1"
    }
    assert calls == [("st_1", "ops")]
    with client.websocket_connect("/studies/st_1/stream?after=0") as ws:
        assert ws.receive_json()["seq"] == 1
        assert ws.receive_json()["seq"] == 2
