"""M5 API: create_app composition with real data_root (not only fakes)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from coscientist.api.app import bind_app_deps, create_app
from coscientist.api.studies import CreateStudyBody, post_studies
from coscientist.core.events.clock import SystemClock
from coscientist.core.events.errors import STUDY_NOT_FOUND_REASON
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import RC3T_THRESHOLDS

_SID_MISSING = "st_" + "d" * 27
_GATE_ENTERED_WAIT_S = 5.0


class _PollSleep:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self, _interval: float) -> None:
        self.count += 1
        await asyncio.sleep(0)


class _GateSleep:
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


async def _create_study(tmp_path: Path) -> str:
    out = await post_studies(
        tmp_path,
        CreateStudyBody(
            request="compose root",
            profile="research",
            catalog_version=3,
            require_hypotheses=False,
            **RC3T_THRESHOLDS,
        ),
        catalog=minimal_test_catalog(),
    )
    return out["study_id"]


def _client(
    tmp_path: Path,
    *,
    sleep: Callable[[float], Awaitable[None]],
) -> TestClient:
    return TestClient(
        create_app(
            bind_app_deps(
                tmp_path,
                catalog=minimal_test_catalog(),
                clock=SystemClock(),
                stream_poll_interval_s=0.01,
                stream_sleep=sleep,
            )
        )
    )


def test_data_root_get_study_ok(tmp_path: Path) -> None:
    sid = asyncio.run(_create_study(tmp_path))
    client = _client(tmp_path, sleep=_PollSleep())
    assert client.get(f"/studies/{sid}").json()["phase"] == "running"


def test_data_root_get_study_missing(tmp_path: Path) -> None:
    client = _client(tmp_path, sleep=_PollSleep())
    resp = client.get(f"/studies/{_SID_MISSING}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == STUDY_NOT_FOUND_REASON


def test_data_root_get_study_malformed(tmp_path: Path) -> None:
    client = _client(tmp_path, sleep=_PollSleep())
    assert client.get("/studies/st_no").status_code == 400


def test_data_root_get_events_ok(tmp_path: Path) -> None:
    sid = asyncio.run(_create_study(tmp_path))
    client = _client(tmp_path, sleep=_PollSleep())
    resp = client.get(f"/studies/{sid}/events", params={"after": "0"})
    assert resp.status_code == 200
    rows = resp.json()
    assert rows
    assert all(row["study_id"] == sid for row in rows)
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))


def test_data_root_get_events_missing(tmp_path: Path) -> None:
    client = _client(tmp_path, sleep=_PollSleep())
    resp = client.get(f"/studies/{_SID_MISSING}/events", params={"after": "0"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == STUDY_NOT_FOUND_REASON
    assert not (tmp_path / _SID_MISSING).exists()


def test_data_root_get_events_malformed_id(tmp_path: Path) -> None:
    client = _client(tmp_path, sleep=_PollSleep())
    assert client.get("/studies/st_no/events", params={"after": "0"}).status_code == 400


def test_data_root_cancel_ok(tmp_path: Path) -> None:
    sid = asyncio.run(_create_study(tmp_path))
    client = _client(tmp_path, sleep=_PollSleep())
    assert client.post(f"/studies/{sid}/cancel", json={"by": "ops"}).json() == {
        "study_id": sid
    }
    assert client.get(f"/studies/{sid}").json()["phase"] == "closed"


def test_data_root_cancel_missing(tmp_path: Path) -> None:
    client = _client(tmp_path, sleep=_PollSleep())
    resp = client.post(f"/studies/{_SID_MISSING}/cancel", json={"by": "ops"})
    assert resp.status_code == 404


def test_data_root_cancel_bad_by(tmp_path: Path) -> None:
    sid = asyncio.run(_create_study(tmp_path))
    client = _client(tmp_path, sleep=_PollSleep())
    assert client.post(f"/studies/{sid}/cancel", json={"by": " "}).status_code == 400


def test_data_root_ws_happy_path(tmp_path: Path) -> None:
    sid = asyncio.run(_create_study(tmp_path))
    client = _client(tmp_path, sleep=_PollSleep())
    with client.websocket_connect(f"/studies/{sid}/stream?after=0") as ws:
        first = ws.receive_json()
        assert first["seq"] == 1
        assert first["study_id"] == sid


def test_data_root_ws_missing_closes_1008(tmp_path: Path) -> None:
    client = _client(tmp_path, sleep=_PollSleep())
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(f"/studies/{_SID_MISSING}/stream?after=0") as ws:
            ws.receive_text()
    assert ei.value.code == 1008
    assert ei.value.reason == STUDY_NOT_FOUND_REASON
    assert not (tmp_path / _SID_MISSING).exists()


def test_data_root_ws_stops_on_disconnect(tmp_path: Path) -> None:
    sid = asyncio.run(_create_study(tmp_path))
    clock = _GateSleep()
    client = _client(tmp_path, sleep=clock)
    with client.websocket_connect(f"/studies/{sid}/stream?after=0") as ws:
        # Drain existing journal so the stream enters poll/sleep.
        while True:
            if clock.entered.wait(timeout=0.05):
                break
            try:
                msg = ws.receive_json()
                assert msg["study_id"] == sid
            except WebSocketDisconnect:
                pytest.fail("ws closed before poll gate")
        assert clock.entered.wait(timeout=_GATE_ENTERED_WAIT_S)
        ws.close()
        clock.release()
    assert clock.count >= 1
