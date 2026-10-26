"""Web-lane E2E: POST /studies/{id}/run → WS stream → verdict + cancel.

In-process (TestClient ASGI + WS) against the real app composition
(``bind_app_deps``) with the CLI cassette surface: the LLM transport is the
existing test ``CassetteTransport`` (patched into ``coscientist.cli.run``),
so the API-started run executes the real CLI ``run_study`` loop end to end.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from coscientist.api.app import bind_app_deps, create_app
from coscientist.api.errors import StudyRunRefuse
from coscientist.api.run import RunStudyBody, StudyRunService, _RunRecord
from coscientist.api.studies import CreateStudyBody, post_studies
from coscientist.checks.catalog import CATALOG_VERSION
from coscientist.core.events.clock import SystemClock
from coscientist.core.events.store import SqliteEventStore
from coscientist.profiles.load import load_profile
from tests.agents.test_adapter_roles import CassetteTransport, _exec_out_json
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.cli.test_run_study import (
    _cli_plan_out_json,
    _default_tools,
    _spec_json,
)
from tests.helpers import RC3T_THRESHOLDS
from tests.llm.test_client import _config, _ok_body

_SID = "st_" + "e" * 27
_SID2 = "st_" + "f" * 27
_RESEARCH_PROFILE = load_profile("research", expected_tool_catalog_version=2)
_RESEARCH_MODEL = _RESEARCH_PROFILE.roles["planner"].model
# Sanity bound for the WS receive loop (the cassette run closes in << 1s).
_MAX_WS_EVENTS = 5000


class _PollSleep:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self, _interval: float) -> None:
        self.count += 1
        await asyncio.sleep(0)


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
                run_wait_s=30.0,
            )
        )
    )


def _write_refs(tmp_path: Path) -> tuple[str, str]:
    """Write llm.json + tools.json cassette refs; return their paths."""
    llm_path = tmp_path / "llm.json"
    llm_path.write_text(_config(max_retries=0).model_dump_json(), encoding="utf-8")
    tools_path = tmp_path / "tools.json"
    tools_path.write_text(json.dumps(_default_tools()), encoding="utf-8")
    return str(llm_path), str(tools_path)


def _run_body(
    tmp_path: Path,
    *,
    profile: str = "research",
    task_text: str = "Build dataset overview and chemical space clustering.",
) -> dict[str, object]:
    llm_ref, tools_ref = _write_refs(tmp_path)
    catalog_ref = tmp_path / "catalog.json"
    catalog_ref.write_text("{}", encoding="utf-8")  # loader patched; ref must exist
    return {
        "task_text": task_text,
        "owner": "api-e2e",
        "deadline_s": 3600.0,
        "lease_ttl_s": 60.0,
        "catalog_version": CATALOG_VERSION,
        "catalog_path": str(catalog_ref),
        "profile": profile,
        "tool_catalog_version": 2,
        "llm_config_path": llm_ref,
        "planner_model": _RESEARCH_MODEL,
        "executor_model": _RESEARCH_MODEL,
        "tz_spec_model": _RESEARCH_MODEL,
        "tools_json_path": tools_ref,
        "mcp_tool_bundle_path": None,
        "tool_call_args_path": None,
        "coder_backend": None,
        "coder_jobs_path": None,
        "wired_roles": [],
        "task_input_paths": [],
        "require_hypotheses": False,
        "confirmations_needed": 1,
        "refutations_needed": 1,
        "refutation_priority": False,
        "supported_needed": 2,
        "max_expansions": 2,
        "finalize_reserve_s": 30,
        "rng_seed": 0,
        "max_llm_calls": 100,
        "max_tokens": 1_000_000,
        "max_tool_calls": 50,
        "max_ticks": 20,
        "hitl_mode": "headless",
    }


def _happy_bodies() -> list[bytes]:
    return [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
    ]


def _patch_cli_surface(
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[bytes],
    *,
    delay_s: float = 0.0,
) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    monkeypatch.setattr(
        "coscientist.cli.run.load_catalog",
        lambda *args, **kwargs: minimal_test_catalog(),
    )

    class _Transport(CassetteTransport):
        async def request(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str],
            body: bytes,
            timeout_s: float,
        ) -> object:
            if delay_s > 0.0:
                await asyncio.sleep(delay_s)
            return await super().request(
                method, url, headers=headers, body=body, timeout_s=timeout_s
            )

    monkeypatch.setattr("coscientist.cli.run.HttpxTransport", lambda: _Transport(bodies))


def _receive_until_final(ws: WebSocketTestSession) -> list[dict[str, object]]:
    """Receive WS frames until ``StudyFinalized`` (terminal verdict event)."""
    events: list[dict[str, object]] = []
    for _ in range(_MAX_WS_EVENTS):
        frame = ws.receive_json()
        events.append(frame)
        if frame["type"] == "StudyFinalized":
            return events
    pytest.fail(f"no StudyFinalized within {_MAX_WS_EVENTS} WS events")


def _run_status_terminal(client: TestClient, sid: str) -> dict[str, object]:
    """Poll GET run until the run task itself is terminal (not just journal)."""
    deadline = time.monotonic() + 10.0
    while True:
        status = client.get(f"/studies/{sid}/run").json()
        if status["run_status"] != "running":
            return status
        if time.monotonic() >= deadline:
            pytest.fail(f"run {sid} still running after 10s: {status!r}")
        time.sleep(0.01)


def test_run_endpoint_e2e_fulfilled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_surface(monkeypatch, _happy_bodies())
    with _client(tmp_path, sleep=_PollSleep()) as client:
        started = client.post(f"/studies/{_SID}/run", json=_run_body(tmp_path))
        assert started.status_code == 200, started.text
        assert started.json() == {"study_id": _SID, "run_status": "running"}

        with client.websocket_connect(f"/studies/{_SID}/stream?after=0") as ws:
            events = _receive_until_final(ws)

        types = [str(ev["type"]) for ev in events]
        assert types[0] == "StudyCreated"
        assert "StudyFinalized" in types
        finalized = next(ev for ev in events if ev["type"] == "StudyFinalized")
        assert finalized["payload"]["verdict"] == "fulfilled"

        status = _run_status_terminal(client, _SID)
        assert status["run_status"] == "done"
        assert status["summary"]["study_id"] == _SID
        assert status["summary"]["phase"] == "closed"
        assert status["summary"]["verdict"] == "fulfilled"

        # Terminal study view + fail-closed re-run refuse (journal exists).
        assert client.get(f"/studies/{_SID}").json()["phase"] == "closed"
        again = client.post(f"/studies/{_SID}/run", json=_run_body(tmp_path))
        assert again.status_code == 400
        assert again.json()["detail"] == "study_journal_already_exists"


def test_run_endpoint_unknown_profile_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_surface(monkeypatch, _happy_bodies())
    with _client(tmp_path, sleep=_PollSleep()) as client:
        body = _run_body(tmp_path, profile="no-such-profile")
        resp = client.post(f"/studies/{_SID}/run", json=body)
        assert resp.status_code == 400
        assert resp.json()["detail"].startswith("study_run_refused:")
        assert not (tmp_path / _SID / "events.db").exists()


def test_run_endpoint_missing_config_ref_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_surface(monkeypatch, _happy_bodies())
    with _client(tmp_path, sleep=_PollSleep()) as client:
        body = _run_body(tmp_path)
        body["llm_config_path"] = str(tmp_path / "missing-llm.json")
        resp = client.post(f"/studies/{_SID}/run", json=body)
        assert resp.status_code == 400
        assert resp.json()["detail"].startswith("config_ref_not_found:")
        assert not (tmp_path / _SID).exists()


def test_run_endpoint_concurrent_second_run_refuses_and_cancel_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Delayed first LLM response keeps the run live while we race + cancel.
    _patch_cli_surface(monkeypatch, _happy_bodies(), delay_s=1.5)
    with _client(tmp_path, sleep=_PollSleep()) as client:
        started = client.post(f"/studies/{_SID}/run", json=_run_body(tmp_path))
        assert started.status_code == 200, started.text
        assert client.get(f"/studies/{_SID}/run").json()["run_status"] == "running"

        second = client.post(f"/studies/{_SID}/run", json=_run_body(tmp_path))
        assert second.status_code == 400
        assert second.json()["detail"] == "study_run_in_progress"

        with client.websocket_connect(f"/studies/{_SID}/stream?after=0") as ws:
            cancelled = client.post(f"/studies/{_SID}/cancel", json={"by": "ops"})
            assert cancelled.status_code == 200, cancelled.text
            events = _receive_until_final(ws)

        types = [str(ev["type"]) for ev in events]
        assert "StudyCancelled" in types
        assert types[-1] == "StudyFinalized"
        status = _run_status_terminal(client, _SID)
        assert status["run_status"] == "cancelled"
        assert client.get(f"/studies/{_SID}").json()["phase"] == "closed"


# --- F1: start-timeout must never orphan a journal (event-keyed re-runs) ---


async def _make_stub_db(tmp_path: Path, sid: str) -> None:
    """Empty journal (schema only, no ``StudyCreated``): the crash-stub shape."""
    store = SqliteEventStore(sid, tmp_path / sid / "events.db", clock=SystemClock())
    await store.close()


async def _visible_journal(tmp_path: Path) -> str:
    """Journal with ``StudyCreated`` and no terminal event; returns the sid."""
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


def _service(tmp_path: Path) -> StudyRunService:
    return StudyRunService(
        tmp_path,
        catalog=minimal_test_catalog(),
        clock=SystemClock(),
        wait_s=0.05,
        poll_interval_s=0.01,
        sleep=_PollSleep(),
    )


async def test_timeout_resolution_cancels_visible_journal(tmp_path: Path) -> None:
    """Run committed StudyCreated before the timeout cancel -> cancel, not refuse."""
    sid = await _visible_journal(tmp_path)
    svc = _service(tmp_path)
    record = _RunRecord(sid, "running", None, None, None)
    resp = await svc._timeout_resolution(sid, record)  # noqa: SLF001
    assert resp == {
        "study_id": sid,
        "run_status": "cancelled",
        "detail": "study_run_start_timeout_cancelled",
    }
    assert record.status == "cancelled"
    store = SqliteEventStore(sid, tmp_path / sid / "events.db", clock=SystemClock())
    try:
        types = [ev.type for ev in await store.read_all()]
    finally:
        await store.close()
    assert "StudyCancelled" in types
    assert types[-1] == "StudyFinalized"
    # And the terminal journal refuses re-runs on the visible event.
    with pytest.raises(StudyRunRefuse, match="study_journal_already_exists"):
        await svc._require_startable(sid)


async def test_timeout_resolution_refuses_rerunnable_stub(tmp_path: Path) -> None:
    """No StudyCreated (empty stub db) -> typed refuse; the stub stays re-runnable."""
    sid = _SID2
    await _make_stub_db(tmp_path, sid)
    svc = _service(tmp_path)
    record = _RunRecord(sid, "cancelled", "study_run_interrupted", None, None)
    with pytest.raises(StudyRunRefuse, match="study_run_start_timeout"):
        await svc._timeout_resolution(sid, record)  # noqa: SLF001
    await svc._require_startable(sid)  # no raise: event-keyed, not file-keyed


def test_run_over_empty_stub_db_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run interrupted pre-create leaves a re-runnable stub (no zombie)."""
    _patch_cli_surface(monkeypatch, _happy_bodies())
    asyncio.run(_make_stub_db(tmp_path, _SID))
    with _client(tmp_path, sleep=_PollSleep()) as client:
        started = client.post(f"/studies/{_SID}/run", json=_run_body(tmp_path))
        assert started.status_code == 200, started.text
        with client.websocket_connect(f"/studies/{_SID}/stream?after=0") as ws:
            events = _receive_until_final(ws)
        finalized = next(ev for ev in events if ev["type"] == "StudyFinalized")
        assert finalized["payload"]["verdict"] == "fulfilled"


# --- F5: single-flight claim must be pre-await (racing same-sid POSTs) ---


async def test_racing_same_sid_posts_single_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two racing same-sid POSTs: the slot is claimed before any await."""
    _patch_cli_surface(monkeypatch, _happy_bodies())
    svc = StudyRunService(
        tmp_path,
        catalog=minimal_test_catalog(),
        clock=SystemClock(),
        wait_s=30.0,
        poll_interval_s=0.01,
        sleep=_PollSleep(),
    )
    body = RunStudyBody.model_validate(_run_body(tmp_path))
    t1 = asyncio.create_task(svc.post_run(_SID, body))
    t2 = asyncio.create_task(svc.post_run(_SID, body))
    results = await asyncio.gather(t1, t2, return_exceptions=True)
    assert results[0] == {"study_id": _SID, "run_status": "running"}
    refusal = results[1]
    assert isinstance(refusal, StudyRunRefuse)
    assert refusal.reason == "study_run_in_progress"
    # Drain the live run so the loop ends clean; status turns done.
    record = svc._runs[_SID]
    assert record.task is not None
    await record.task
    assert svc.run_status(_SID)["run_status"] == "done"


def test_racing_same_sid_posts_second_gets_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP-level race through ONE shared app: second POST → 400 in_progress."""
    _patch_cli_surface(monkeypatch, _happy_bodies())
    # Suspend the first POST inside the journal check so the second lands
    # mid-await (the exact window the pre-await claim closes).
    real_read_all = SqliteEventStore.read_all

    async def slow_read_all(store: SqliteEventStore) -> object:
        await asyncio.sleep(0.3)
        return await real_read_all(store)

    monkeypatch.setattr(SqliteEventStore, "read_all", slow_read_all)
    results: dict[str, tuple[int, str]] = {}

    with _client(tmp_path, sleep=_PollSleep()) as shared:
        def _post(tag: str) -> None:
            resp = shared.post(f"/studies/{_SID}/run", json=_run_body(tmp_path))
            results[tag] = (resp.status_code, str(resp.json().get("detail")))

        t_a = threading.Thread(target=_post, args=("a",))
        t_b = threading.Thread(target=_post, args=("b",))
        t_a.start()
        time.sleep(0.05)
        t_b.start()
        t_a.join()
        t_b.join()
        # Drain the winner before the portal closes.
        _run_status_terminal(shared, _SID)

    codes = sorted(code for code, _ in results.values())
    assert codes == [200, 400]
    assert "study_run_in_progress" in [detail for _, detail in results.values()]
