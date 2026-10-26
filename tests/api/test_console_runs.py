"""Preset security, durable launch identity and single-owner lifecycle."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from coscientist.api.app import bind_app_deps, create_app
from coscientist.api.console_runs import ConsoleRunService, LaunchBody
from coscientist.api.errors import StudyRunRefuse
from coscientist.api.presets import PresetDocument, PresetRefuse, load_run_presets
from coscientist.api.run import _RunRecord
from coscientist.core.events.clock import FakeClock
from tests.api.test_console import _write_journal
from tests.api.test_run_endpoint import _happy_bodies, _patch_cli_surface, _run_body, _service
from tests.checks.minimal_test_catalog import minimal_test_catalog


def _presets(tmp_path):
    config = _run_body(tmp_path)
    del config["task_text"]
    config["hitl_mode"] = "interactive"
    document = {"presets": [{
        "id": "research", "label": "Исследование", "description": "Два инструмента", "config": config,
    }]}
    path = tmp_path / "presets.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_run_presets(path), document


def _request(key="key", task="Exact human task"):
    return LaunchBody(preset_id="research", task_text=task, idempotency_key=key)


def test_preset_schema_excludes_task_and_metadata_hides_paths(tmp_path):
    presets, document = _presets(tmp_path)
    assert "task_text" not in PresetDocument.model_json_schema()["$defs"]["RunStudySettings"]["properties"]
    public = json.dumps(presets["research"].metadata())
    assert str(tmp_path) not in public and "llm_config_path" not in public
    document["presets"][0]["config"]["task_text"] = "must not be configured"
    with pytest.raises(ValidationError):
        PresetDocument.model_validate(document)


@pytest.mark.asyncio
async def test_same_key_concurrency_and_different_body_fail_closed(tmp_path, monkeypatch):
    presets, _ = _presets(tmp_path)
    root = tmp_path / "data"
    runs = _service(root)
    calls = []

    async def start(sid, body):
        calls.append((sid, body))
        await asyncio.sleep(0)
        runs._runs[sid] = _RunRecord(sid, "running", None, None, None, None)

    monkeypatch.setattr(runs, "post_run", start)
    service = ConsoleRunService(root, runs=runs, presets=presets, wait_s=1)
    responses = await asyncio.gather(*(service.launch(_request()) for _ in range(6)))
    assert len(calls) == 1 and len({response["study_id"] for response in responses}) == 1
    assert calls[0][1].task_text == "Exact human task"
    with pytest.raises(PresetRefuse, match="idempotency_conflict"):
        await service.launch(_request(task="different"))
    await service.launch(_request(key="other"))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_restart_never_restarts_visible_journal(tmp_path, monkeypatch):
    presets, _ = _presets(tmp_path)
    root = tmp_path / "data"
    runs = _service(root)

    async def start(sid, body):
        await _write_journal(root, sid, verdict=None)
        runs._runs[sid] = _RunRecord(sid, "running", None, None, None, None)

    monkeypatch.setattr(runs, "post_run", start)
    first = ConsoleRunService(root, runs=runs, presets=presets, wait_s=.01)
    response = await first.launch(_request())
    fresh_runs = _service(root)

    async def forbidden(*args):
        raise AssertionError("restart must never launch research")

    monkeypatch.setattr(fresh_runs, "post_run", forbidden)
    fresh = ConsoleRunService(root, runs=fresh_runs, presets=presets, wait_s=.01)
    replay = await fresh.launch(_request())
    assert replay["study_id"] == response["study_id"]
    assert replay["run_status"] == "owner_unavailable" and not replay["can_cancel"]


@pytest.mark.asyncio
async def test_failure_is_durable_and_does_not_leak_config(tmp_path, monkeypatch):
    presets, _ = _presets(tmp_path)
    runs = _service(tmp_path / "data")
    calls = []

    async def fail(sid, body):
        calls.append(sid)
        raise StudyRunRefuse("credential-secret /private/server/config")

    monkeypatch.setattr(runs, "post_run", fail)
    for _ in range(2):
        service = ConsoleRunService(tmp_path / "data", runs=runs, presets=presets, wait_s=.01)
        with pytest.raises(PresetRefuse, match="^launch_refused$"):
            await service.launch(_request())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_browser_disconnect_keeps_single_launch_and_pending_owner_loss_refuses(tmp_path, monkeypatch):
    presets, _ = _presets(tmp_path)
    root = tmp_path / "data"
    runs = _service(root)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def start(sid, body):
        calls.append(sid)
        entered.set()
        await release.wait()
        runs._runs[sid] = _RunRecord(sid, "running", None, None, None, None)

    monkeypatch.setattr(runs, "post_run", start)
    service = ConsoleRunService(root, runs=runs, presets=presets, wait_s=.001)
    request = asyncio.create_task(service.launch(_request()))
    await entered.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    fresh = ConsoleRunService(root, runs=_service(root), presets=presets, wait_s=.001)
    with pytest.raises(PresetRefuse, match="owner_unavailable_no_restart"):
        await fresh.launch(_request())
    release.set()
    assert (await service.launch(_request()))["run_status"] == "running"
    assert len(calls) == 1


def test_changed_preset_reference_refuses_without_launch(tmp_path):
    presets, _ = _presets(tmp_path)
    (tmp_path / "tools.json").write_text("changed", encoding="utf-8")
    with pytest.raises(PresetRefuse, match="configuration_changed"):
        presets["research"].body("task")


@pytest.mark.asyncio
async def test_unconfigured_and_unknown_presets_are_typed(tmp_path):
    presets, _ = _presets(tmp_path)
    runs = _service(tmp_path / "data")
    disabled = ConsoleRunService(tmp_path / "data", runs=runs, presets={}, wait_s=.01)
    with pytest.raises(PresetRefuse, match="not_configured"):
        await disabled.launch(_request())
    configured = ConsoleRunService(tmp_path / "data", runs=runs, presets=presets, wait_s=.01)
    with pytest.raises(PresetRefuse, match="preset_unknown"):
        await configured.launch(_request().model_copy(update={"preset_id": "missing"}))


@pytest.mark.parametrize("state", ["failed", "refused", "cancelled"])
def test_known_owner_failure_without_journal_is_preserved_and_sanitized(tmp_path, state):
    runs = _service(tmp_path)
    sid = "st_" + "a" * 27
    runs._runs[sid] = _RunRecord(sid, state, "secret-credential /private/config", None, None, None)
    service = ConsoleRunService(tmp_path, runs=runs, presets={}, wait_s=.01)
    status = service.status(sid)
    assert status["run_status"] == state
    assert status["journal_finalized"] is False and "verdict" not in status
    assert "secret" not in json.dumps(status) and "/private" not in json.dumps(status)


@pytest.mark.parametrize("action", ["accept", "cancel"])
def test_browser_launch_real_fixture_runner_pauses_then_accepts(tmp_path, monkeypatch, action):
    presets, _ = _presets(tmp_path)
    _patch_cli_surface(monkeypatch, _happy_bodies())
    deps = bind_app_deps(tmp_path / "data", catalog=minimal_test_catalog(), clock=FakeClock(),
                         stream_poll_interval_s=.01, stream_sleep=asyncio.sleep, run_wait_s=2)
    with TestClient(create_app(deps, run_presets=presets)) as client:
        launched = client.post("/console/runs", json=_request().model_dump())
        assert launched.status_code == 200, launched.text
        sid = launched.json()["study_id"]
        assert client.post("/console/runs", json=_request().model_dump()).json()["study_id"] == sid
        for _ in range(100):
            rows = client.get(f"/console/studies/{sid}/hitl").json()
            if rows and rows[0]["can_answer"]:
                break
        assert rows and rows[0]["can_answer"]
        row = rows[0]
        if action == "cancel":
            cancellation = client.post(f"/studies/{sid}/cancel", json={"by": "console-human"})
            assert cancellation.status_code == 200, cancellation.text
            events = client.get(f"/console/studies/{sid}/events?after=0").json()
            assert sum(event["type"] == "StudyFinalized" for event in events) == 1
            assert not any(event["type"] == "HitlDecided" for event in events)
            replay = client.get(f"/console/studies/{sid}/hitl").json()
            assert replay[0]["status"] == "study_closed" and not replay[0]["can_answer"]
            assert client.post(f"/studies/{sid}/cancel", json={"by": "console-human"}).status_code == 200
            assert client.get(f"/console/studies/{sid}/events?after=0").json() == events
            return
        answer = client.post(f"/studies/{sid}/hitl/{row['request_id']}", json={
            "decision": "accept", "opened_seq": row["opened_seq"], "context_sha256": row["prompt"]["context_sha256"],
        })
        assert answer.status_code == 200, answer.text
        for _ in range(100):
            status = client.get(f"/console/runs/{sid}").json()
            if status["run_status"] != "running":
                break
        assert status["run_status"] == "done" and status["verdict"] == "fulfilled"
