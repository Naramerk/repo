"""M5 API: FastAPI routes + WS stream (fakes; extra ``api``)."""

from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from coscientist.api.app import (
    WS_CLOSE_REASON_MAX_BYTES,
    AppDeps,
    create_app,
    encode_ws_close_reason,
)
from coscientist.api.cancel import bind_cancel_study
from coscientist.api.errors import AfterSeqRefuse, StreamConfigRefuse, WsCloseReasonRefuse
from coscientist.api.run import StudyRunService
from coscientist.api.stream import require_poll_interval_s, stream_study_events
from coscientist.api.views import parse_after_query
from coscientist.core.events.clock import SystemClock
from coscientist.core.events.errors import STUDY_NOT_FOUND_REASON, StudyNotFound
from coscientist.core.events.events import StoredEvent, UnstampedEvent
from coscientist.core.events.ports import EventStore
from coscientist.core.execution.domain import StudyView
from tests.checks.minimal_test_catalog import minimal_test_catalog

_SID = "st_" + "a" * 27
_SID_MISSING = "st_" + "b" * 27
_SID_FOREIGN = "st_" + "c" * 27
# Wall-clock bound only for the disconnect gate handshake in TestClient.
_GATE_ENTERED_WAIT_S = 5.0


class _Studies:
    def study_view(self, study_id: str) -> StudyView:
        if study_id != _SID:
            raise StudyNotFound(study_id, reason=STUDY_NOT_FOUND_REASON)
        return StudyView(phase="running")


class _Store:
    """EventStore double (full Protocol surface; typed fields)."""

    study_id: str
    owner: str | None
    epoch: int
    owner_nonce: str | None

    def __init__(self, events: list[StoredEvent]) -> None:
        self.study_id = _SID
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


class _Ws:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send_json(self, data: object) -> None:
        self.sent.append(data)


class _PollSleep:
    """Injected clock: count polls, yield to the loop (no wall-clock wait)."""

    def __init__(self) -> None:
        self.count = 0

    async def __call__(self, _interval: float) -> None:
        self.count += 1
        await asyncio.sleep(0)


class _GateSleep:
    """Block after the first poll so the test can close the socket exactly once."""

    def __init__(self) -> None:
        self.count = 0
        self.entered = threading.Event()
        self._gate = threading.Event()

    async def __call__(self, _interval: float) -> None:
        self.count += 1
        self.entered.set()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._gate.wait)

    def release(self) -> None:
        self._gate.set()


class _DisconnectPollState:
    """Typed poll counter for ``is_disconnected`` in the stream-helper unit test."""

    polls: int = 0


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
    tmp_path: Path,
    calls: list[tuple[str, str]],
    *,
    store: EventStore,
    sleep: Callable[[float], Awaitable[None]],
) -> TestClient:
    """Build TestClient. ``store`` and ``sleep`` required — one path, no defaults."""

    async def _cancel(sid: str, by: str) -> dict[str, str]:
        if sid != _SID:
            raise StudyNotFound(sid, reason=STUDY_NOT_FOUND_REASON)
        calls.append((sid, by))
        return {"study_id": sid}

    def _store_for(sid: str) -> EventStore:
        if sid != _SID:
            raise StudyNotFound(sid, reason=STUDY_NOT_FOUND_REASON)
        return store

    return TestClient(
        create_app(
            AppDeps(
                studies=_Studies(),
                event_store_for=_store_for,
                cancel_study=_cancel,
                run_studies=StudyRunService(
                    tmp_path,
                    catalog=minimal_test_catalog(),
                    clock=SystemClock(),
                    wait_s=1.0,
                    poll_interval_s=0.01,
                    sleep=sleep,
                ),
                stream_poll_interval_s=0.01,
                stream_sleep=sleep,
            )
        )
    )


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (0.0, r"poll_interval_s_must_be_positive_finite_got_0\.0"),
        (-1.0, r"poll_interval_s_must_be_positive_finite_got_-1\.0"),
    ],
    ids=["zero", "neg_float"],
)
def test_poll_interval_refuse_non_positive(value: float, match: str) -> None:
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


def test_get_study_ok(tmp_path: Path) -> None:
    client = _make_app(tmp_path, [], store=_Store([_ev(1)]), sleep=_PollSleep())
    assert client.get(f"/studies/{_SID}").json()["phase"] == "running"


def test_get_study_missing(tmp_path: Path) -> None:
    client = _make_app(tmp_path, [], store=_Store([_ev(1)]), sleep=_PollSleep())
    assert client.get(f"/studies/{_SID_MISSING}").status_code == 404


def test_get_study_malformed(tmp_path: Path) -> None:
    client = _make_app(tmp_path, [], store=_Store([_ev(1)]), sleep=_PollSleep())
    assert client.get("/studies/st_no").status_code == 400


def test_get_events_ok(tmp_path: Path) -> None:
    client = _make_app(tmp_path, [], store=_Store([_ev(1), _ev(2)]), sleep=_PollSleep())
    ev = client.get(f"/studies/{_SID}/events", params={"after": "0"})
    assert ev.status_code == 200
    assert [row["seq"] for row in ev.json()] == [1, 2]


def test_get_events_foreign(tmp_path: Path) -> None:
    client = _make_app(tmp_path, [], store=_Store([_ev(1)]), sleep=_PollSleep())
    assert client.get(f"/studies/{_SID_FOREIGN}/events", params={"after": "0"}).status_code == 404


def test_get_events_malformed_id(tmp_path: Path) -> None:
    client = _make_app(tmp_path, [], store=_Store([_ev(1)]), sleep=_PollSleep())
    assert client.get("/studies/st_no/events", params={"after": "0"}).status_code == 400


def test_parse_after_query_ok() -> None:
    assert parse_after_query("0") == 0


def test_parse_after_query_required() -> None:
    with pytest.raises(AfterSeqRefuse, match="after_query_required"):
        parse_after_query(None)


def test_parse_after_query_whitespace() -> None:
    with pytest.raises(AfterSeqRefuse, match="after_query_whitespace"):
        parse_after_query(" 1")


@pytest.mark.parametrize(
    "raw",
    ["nope", "+1", "1_000"],
    ids=["alpha", "plus_sign", "underscore"],
)
def test_parse_after_query_non_digits(raw: str) -> None:
    with pytest.raises(AfterSeqRefuse, match="after_query_non_digits"):
        parse_after_query(raw)


def test_cancel_ok(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    client = _make_app(tmp_path, calls, store=_Store([_ev(1)]), sleep=_PollSleep())
    assert client.post(f"/studies/{_SID}/cancel", json={"by": "ops"}).json() == {"study_id": _SID}
    assert calls == [(_SID, "ops")]


def test_cancel_missing_study_via_stub(tmp_path: Path) -> None:
    client = _make_app(tmp_path, [], store=_Store([_ev(1)]), sleep=_PollSleep())
    assert client.post(f"/studies/{_SID_MISSING}/cancel", json={"by": "ops"}).status_code == 404


def _make_bind_cancel_app(tmp_path: Path) -> TestClient:
    def _store_missing(sid: str) -> EventStore:
        raise StudyNotFound(sid, reason=STUDY_NOT_FOUND_REASON)

    return TestClient(
        create_app(
            AppDeps(
                studies=_Studies(),
                event_store_for=_store_missing,
                cancel_study=bind_cancel_study(tmp_path, catalog=minimal_test_catalog()),
                run_studies=StudyRunService(
                    tmp_path,
                    catalog=minimal_test_catalog(),
                    clock=SystemClock(),
                    wait_s=1.0,
                    poll_interval_s=0.01,
                    sleep=_PollSleep(),
                ),
                stream_poll_interval_s=0.01,
                stream_sleep=_PollSleep(),
            )
        )
    )


def test_cancel_bind_missing_study(tmp_path: Path) -> None:
    client = _make_bind_cancel_app(tmp_path)
    assert client.post(f"/studies/{_SID_MISSING}/cancel", json={"by": "ops"}).status_code == 404


def test_cancel_bind_bad_by(tmp_path: Path) -> None:
    client = _make_bind_cancel_app(tmp_path)
    assert client.post(f"/studies/{_SID}/cancel", json={"by": " "}).status_code == 400


def test_ws_happy_path(tmp_path: Path) -> None:
    client = _make_app(tmp_path, [], store=_Store([_ev(1), _ev(2)]), sleep=_PollSleep())
    with client.websocket_connect(f"/studies/{_SID}/stream?after=0") as ws:
        assert ws.receive_json()["seq"] == 1
        assert ws.receive_json()["seq"] == 2


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (f"/studies/{_SID}/stream", "after_query_required"),
        (f"/studies/{_SID}/stream?after=%201", "after_query_whitespace"),
        (f"/studies/{_SID}/stream?after=nope", "after_query_non_digits"),
        (f"/studies/{_SID_FOREIGN}/stream?after=0", STUDY_NOT_FOUND_REASON),
    ],
    ids=["no_after", "whitespace", "non_digits", "foreign"],
)
def test_ws_refuse_closes_1008(tmp_path: Path, path: str, reason: str) -> None:
    client = _make_app(tmp_path, [], store=_Store([_ev(1)]), sleep=_PollSleep())
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(path) as ws:
            ws.receive_text()
    assert ei.value.code == 1008
    assert ei.value.reason == reason


def test_ws_refuse_long_after_closes_1008(tmp_path: Path) -> None:
    """Long client ``after`` must close 1008 — never blow encode_ws_close_reason."""
    long_after = "x" * 200
    client = _make_app(tmp_path, [], store=_Store([_ev(1)]), sleep=_PollSleep())
    path = f"/studies/{_SID}/stream?after={long_after}"
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(path) as ws:
            ws.receive_text()
    assert ei.value.code == 1008
    assert ei.value.reason == "after_query_non_digits"


def test_encode_ws_close_reason_ok() -> None:
    assert encode_ws_close_reason("short") == "short"


def test_encode_ws_close_reason_exceeds_limit() -> None:
    oversized = "x" * (WS_CLOSE_REASON_MAX_BYTES + 1)
    size = len(oversized.encode("utf-8"))
    match = rf"ws_close_reason_exceeds_{WS_CLOSE_REASON_MAX_BYTES}_bytes_got_{size}"
    with pytest.raises(WsCloseReasonRefuse, match=match):
        encode_ws_close_reason(oversized)


def test_ws_stops_on_client_disconnect(tmp_path: Path) -> None:
    clock = _GateSleep()
    client = _make_app(tmp_path, [], store=_Store([]), sleep=clock)
    with client.websocket_connect(f"/studies/{_SID}/stream?after=0") as ws:
        assert clock.entered.wait(timeout=_GATE_ENTERED_WAIT_S)
        ws.close()
        clock.release()
    assert clock.count == 1


@pytest.mark.asyncio
async def test_stream_helper_stops_when_disconnected() -> None:
    ws = _Ws()
    state = _DisconnectPollState()

    def is_disconnected() -> bool:
        return state.polls >= 2

    async def sleep(_interval: float) -> None:
        state.polls += 1

    store: EventStore = _Store([])
    await stream_study_events(
        ws,
        store,
        study_id=_SID,
        after=0,
        poll_interval_s=0.01,
        sleep=sleep,
        is_disconnected=is_disconnected,
    )
    assert state.polls == 2
    assert ws.sent == []


class _BoomStore(_Store):
    """EventStore fake: first read_after fails (exercises stream_route finally)."""

    async def read_after(self, seq: int) -> list[StoredEvent]:
        raise RuntimeError("stream_store_boom")


def test_ws_store_error_propagates(tmp_path: Path) -> None:
    """Injected store error propagates through stream_route (finally cancels watcher)."""
    client = _make_app(tmp_path, [], store=_BoomStore([]), sleep=_PollSleep())
    with pytest.raises(RuntimeError, match="stream_store_boom"):
        with client.websocket_connect(f"/studies/{_SID}/stream?after=0") as ws:
            ws.receive_text()
