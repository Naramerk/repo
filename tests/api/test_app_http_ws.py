"""M5 API: FastAPI routes + WS stream (fakes; extra ``api``)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from coscientist.api.app import AppDeps, create_app
from coscientist.api.cancel import bind_cancel_study, require_cancel_by
from coscientist.api.errors import StreamConfigRefuse
from coscientist.api.stream import require_poll_interval_s, stream_study_events
from coscientist.api.views import require_study_id
from coscientist.core.events.errors import STUDY_NOT_FOUND_REASON, StudyNotFound
from coscientist.core.events.events import StoredEvent
from coscientist.core.execution.domain import StudyView
from tests.checks.minimal_test_catalog import minimal_test_catalog

_SID = "st_" + "a" * 27
_SID_MISSING = "st_" + "b" * 27
_SID_FOREIGN = "st_" + "c" * 27


class _Studies:
    def study_view(self, study_id: str) -> StudyView:
        if study_id != _SID:
            raise StudyNotFound(study_id, reason=STUDY_NOT_FOUND_REASON)
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


class _Ws:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send_json(self, data: object) -> None:
        self.sent.append(data)


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
        # Mirror bind_cancel_study → post_study_cancel gates (route does not re-validate).
        sid_ok = require_study_id(sid)
        by_ok = require_cancel_by(by)
        if sid_ok != _SID:
            raise StudyNotFound(sid_ok, reason=STUDY_NOT_FOUND_REASON)
        calls.append((sid_ok, by_ok))
        return {"study_id": sid_ok}

    def _store_for(sid: str) -> _Store:
        if sid != _SID:
            raise StudyNotFound(sid, reason=STUDY_NOT_FOUND_REASON)
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


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (0, r"poll_interval_s_must_be_positive_finite_got_0"),
        (-1.0, r"poll_interval_s_must_be_positive_finite_got_-1\.0"),
    ],
    ids=["zero", "neg_float"],
)
def test_poll_interval_refuse_non_positive(value: object, match: str) -> None:
    with pytest.raises(StreamConfigRefuse, match=match):
        require_poll_interval_s(value)


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (math.inf, r"poll_interval_s_must_be_positive_finite_got_inf"),
        (math.nan, r"poll_interval_s_must_be_positive_finite_got_nan"),
    ],
    ids=["inf", "nan"],
)
def test_poll_interval_refuse_non_finite(value: float, match: str) -> None:
    with pytest.raises(StreamConfigRefuse, match=match):
        require_poll_interval_s(value)


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (True, r"poll_interval_s_must_be_number_got_bool:True"),
        ("x", r"poll_interval_s_must_be_number_got_str:'x'"),
    ],
    ids=["bool", "str"],
)
def test_poll_interval_refuse_non_number(value: object, match: str) -> None:
    with pytest.raises(StreamConfigRefuse, match=match):
        require_poll_interval_s(value)


def test_get_study_ok_and_missing() -> None:
    client = _make_app([_ev(1)], [])
    assert client.get(f"/studies/{_SID}").json()["phase"] == "running"
    assert client.get(f"/studies/{_SID_MISSING}").status_code == 404
    assert client.get("/studies/st_no").status_code == 400


def test_get_events_ok() -> None:
    client = _make_app([_ev(1), _ev(2)], [])
    ev = client.get(f"/studies/{_SID}/events", params={"after": 0})
    assert ev.status_code == 200
    assert [row["seq"] for row in ev.json()] == [1, 2]


def test_get_events_foreign_and_malformed() -> None:
    client = _make_app([_ev(1)], [])
    assert client.get(f"/studies/{_SID_FOREIGN}/events", params={"after": 0}).status_code == 404
    assert client.get("/studies/st_no/events", params={"after": 0}).status_code == 400


def test_cancel_ok() -> None:
    calls: list[tuple[str, str]] = []
    client = _make_app([_ev(1)], calls)
    assert client.post(f"/studies/{_SID}/cancel", json={"by": "ops"}).json() == {"study_id": _SID}
    assert calls == [(_SID, "ops")]


def test_cancel_bad_by_and_missing_study() -> None:
    client = _make_app([_ev(1)], [])
    assert client.post(f"/studies/{_SID}/cancel", json={"by": " "}).status_code == 400
    assert client.post(f"/studies/{_SID_MISSING}/cancel", json={"by": "ops"}).status_code == 404


def test_cancel_via_bind_cancel_study(tmp_path: object) -> None:
    from pathlib import Path

    root = Path(tmp_path)
    catalog = minimal_test_catalog()

    def _store_missing(sid: str) -> _Store:
        raise StudyNotFound(sid, reason=STUDY_NOT_FOUND_REASON)

    client = TestClient(
        create_app(
            AppDeps(
                studies=_Studies(),
                event_store_for=_store_missing,
                cancel_study=bind_cancel_study(root, catalog=catalog),
                stream_poll_interval_s=0.01,
            )
        )
    )
    assert client.post(f"/studies/{_SID_MISSING}/cancel", json={"by": "ops"}).status_code == 404
    assert client.post(f"/studies/{_SID}/cancel", json={"by": " "}).status_code == 400


def test_ws_happy_path() -> None:
    client = _make_app([_ev(1), _ev(2)], [])
    with client.websocket_connect(f"/studies/{_SID}/stream?after=0") as ws:
        assert ws.receive_json()["seq"] == 1
        assert ws.receive_json()["seq"] == 2


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (f"/studies/{_SID}/stream", "after_query_required"),
        (f"/studies/{_SID}/stream?after=%201", "after_query_whitespace"),
        (f"/studies/{_SID}/stream?after=nope", "after_query_invalid:ValueError"),
        (f"/studies/{_SID_FOREIGN}/stream?after=0", STUDY_NOT_FOUND_REASON),
    ],
    ids=["no_after", "whitespace", "non_int", "foreign"],
)
def test_ws_refuse_closes_1008(path: str, reason: str) -> None:
    client = _make_app([_ev(1)], [])
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(path) as ws:
            ws.receive_text()
    assert ei.value.code == 1008
    assert ei.value.reason == reason


def test_ws_stops_on_client_disconnect() -> None:
    client = _make_app([], [])
    with client.websocket_connect(f"/studies/{_SID}/stream?after=0"):
        pass


@pytest.mark.asyncio
async def test_stream_helper_stops_when_disconnected() -> None:
    ws = _Ws()
    polls = {"n": 0}

    def is_disconnected() -> bool:
        return polls["n"] >= 2

    async def sleep(_interval: float) -> None:
        polls["n"] += 1

    await stream_study_events(
        ws,
        _Store([]),
        study_id=_SID,
        after=0,
        poll_interval_s=0.01,
        sleep=sleep,
        is_disconnected=is_disconnected,
    )
    assert polls["n"] == 2
    assert ws.sent == []
