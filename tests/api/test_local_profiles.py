"""Local catalog persistence and profile-pinned, single-owner research launches."""
import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from coscientist.api.app import bind_app_deps, create_app
from coscientist.api.console import list_studies
from coscientist.api.console_runs import ConsoleRunService
from coscientist.api.local_profiles import LocalProfiles, ProfileRefuse
from coscientist.api.presets import PresetRefuse
from coscientist.api.run import _RunRecord
from coscientist.api.run_binding import record_active_journal
from coscientist.core.events.clock import FakeClock
from tests.api.test_console import _write_journal
from tests.api.test_console_runs import _presets, _request
from tests.api.test_run_endpoint import _happy_bodies, _patch_cli_surface, _service
from tests.checks.minimal_test_catalog import minimal_test_catalog


def test_profiles_are_durable_unique_and_reading_does_not_create_catalog(tmp_path):
    catalog = LocalProfiles(tmp_path)
    assert catalog.list_profiles() == []
    assert not (tmp_path / "console-profiles.sqlite3").exists()
    profile = catalog.create("  Cafe\u0301   User  ")
    assert profile["nickname"] == "Café User"
    assert LocalProfiles(tmp_path).get(profile["id"]) == profile
    with pytest.raises(ProfileRefuse, match="nickname_exists"):
        catalog.create("CAFÉ USER")
    for invalid in ("  ", "x" * 65, "name\x00"):
        with pytest.raises(ProfileRefuse, match="nickname_invalid"):
            catalog.create(invalid)
    with pytest.raises(ProfileRefuse, match="profile_not_found"):
        catalog.get("lp_" + "0" * 32)
    with ThreadPoolExecutor(max_workers=4) as pool:
        def create_once(_):
            try:
                return catalog.create("Concurrent")["id"]
            except ProfileRefuse as exc:
                assert exc.reason == "profile_nickname_exists"
                return None
        results = list(pool.map(create_once, range(4)))
    assert len([value for value in results if value is not None]) == 1


def test_store_never_creates_or_keeps_removed_profile_settings_table(tmp_path):
    catalog = LocalProfiles(tmp_path)
    catalog.create("Settings Probe")
    with sqlite3.connect(tmp_path / "console-profiles.sqlite3") as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == {"local_profiles", "profile_studies"}
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    with sqlite3.connect(legacy / "console-profiles.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE local_profiles (
                id TEXT PRIMARY KEY, nickname TEXT NOT NULL,
                nickname_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE profile_settings (
                profile_id TEXT NOT NULL REFERENCES local_profiles(id),
                revision INTEGER NOT NULL CHECK(revision > 0),
                catalog_digest TEXT NOT NULL, settings_json TEXT NOT NULL, settings_digest TEXT NOT NULL,
                PRIMARY KEY(profile_id, revision));
            PRAGMA user_version=2;
            """
        )
    LocalProfiles(legacy).create("Legacy Probe")
    with sqlite3.connect(legacy / "console-profiles.sqlite3") as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == {"local_profiles", "profile_studies"}


@pytest.mark.asyncio
async def test_explicit_attach_resolves_only_actual_owner_binding_and_never_guesses(tmp_path):
    catalog = LocalProfiles(tmp_path)
    first, second = catalog.create("First"), catalog.create("Second")
    sid, legacy = "st_" + "a" * 27, "legacy-cli-study"
    child, unrelated = sid + "-r1", sid + "-r2"
    for name in (sid, child, unrelated, legacy):
        await _write_journal(tmp_path, name, verdict=None)
    record_active_journal(tmp_path, sid, child)
    assert catalog.study_rows(first["id"], list_studies(tmp_path)) == []
    assert catalog.attach(first["id"], child)["study_id"] == sid
    catalog.attach(first["id"], sid)
    catalog.attach(first["id"], legacy)
    rows = catalog.study_rows(first["id"], list_studies(tmp_path))
    assert {row["study_id"] for row in rows} == {sid, child, legacy}
    assert next(row for row in rows if row["study_id"] == child)["root_study_id"] == sid
    with pytest.raises(ProfileRefuse, match="profile_study_conflict"):
        catalog.attach(second["id"], child)
    assert len(list_studies(tmp_path)) == 4


@pytest.mark.asyncio
async def test_launch_profile_is_reserved_and_bound_before_dispatch_with_one_owner(tmp_path, monkeypatch):
    presets, _ = _presets(tmp_path)
    root = tmp_path / "data"
    runs = _service(root)
    service = ConsoleRunService(root, runs=runs, presets=presets, wait_s=1)
    profile = service.profiles.create("Researcher")
    request = _request().model_copy(update={"profile_id": profile["id"]})
    calls = []

    async def start(sid, body):
        mapping_path = next(path for path in (root / "idempotency/console-runs").iterdir() if len(path.name) == 64)
        mapping = json.loads(mapping_path.read_text())
        assert mapping["context"] == {"profile_id": profile["id"]}
        assert service.profiles.study_rows(profile["id"], [{"study_id": sid}])
        calls.append(sid)
        await _write_journal(root, sid, verdict=None)
        runs._runs[sid] = _RunRecord(sid, "running", None, None, None, None, None)

    monkeypatch.setattr(runs, "post_run", start)
    responses = await asyncio.gather(*(service.launch(request) for _ in range(6)))
    sid = responses[0]["study_id"]
    assert calls == [sid]
    other = service.profiles.create("Other")
    with pytest.raises(PresetRefuse, match="idempotency_conflict"):
        await service.launch(request.model_copy(update={"profile_id": other["id"]}))
    with pytest.raises(ProfileRefuse, match="profile_not_found"):
        await service.launch(request.model_copy(update={"profile_id": "lp_" + "0" * 32}))
    # Missing SQLite association after a partial cross-store failure is repairable
    # from the durable reservation; replay must not create a second research run.
    with sqlite3.connect(root / "console-profiles.sqlite3") as connection:
        connection.execute("DELETE FROM profile_studies")
    fresh_runs = _service(root)

    async def forbidden(*args):
        raise AssertionError("replay must never restart a research owner")

    monkeypatch.setattr(fresh_runs, "post_run", forbidden)
    fresh = ConsoleRunService(root, runs=fresh_runs, presets=presets, wait_s=.01)
    assert (await fresh.launch(request))["run_status"] == "owner_unavailable"
    assert len(fresh.profiles.study_rows(profile["id"], list_studies(root))) == 1


@pytest.mark.asyncio
async def test_profile_bind_failure_cannot_dispatch_and_failure_remains_durable(tmp_path, monkeypatch):
    presets, _ = _presets(tmp_path)
    root = tmp_path / "data"
    runs = _service(root)
    service = ConsoleRunService(root, runs=runs, presets=presets, wait_s=.01)
    profile = service.profiles.create("Profile")
    request = _request().model_copy(update={"profile_id": profile["id"]})

    def fail(*args):
        raise ProfileRefuse("profile_store_unavailable", 503)

    async def forbidden(*args):
        raise AssertionError("association failure must not dispatch research")

    monkeypatch.setattr(service.profiles, "bind_launch", fail)
    monkeypatch.setattr(runs, "post_run", forbidden)
    with pytest.raises(ProfileRefuse, match="profile_store_unavailable"):
        await service.launch(request)
    fresh = ConsoleRunService(root, runs=runs, presets=presets, wait_s=.01)
    with pytest.raises(PresetRefuse, match="launch_refused"):
        await fresh.launch(request)
    assert list_studies(root) == []


@pytest.mark.asyncio
async def test_crash_after_reservation_repairs_metadata_without_restarting_research(tmp_path, monkeypatch):
    presets, _ = _presets(tmp_path)
    root = tmp_path / "data"
    runs = _service(root)
    service = ConsoleRunService(root, runs=runs, presets=presets, wait_s=.001)
    profile = service.profiles.create("Profile")
    request = _request().model_copy(update={"profile_id": profile["id"]})

    class SimulatedCrash(BaseException):
        pass

    def crash(*args):
        raise SimulatedCrash()

    async def forbidden(*args):
        raise AssertionError("crashed owner must not be automatically restarted")

    monkeypatch.setattr(service.profiles, "bind_launch", crash)
    monkeypatch.setattr(runs, "post_run", forbidden)
    with pytest.raises(SimulatedCrash):
        await service.launch(request)
    fresh = ConsoleRunService(root, runs=runs, presets=presets, wait_s=.001)
    with pytest.raises(PresetRefuse, match="owner_unavailable_no_restart"):
        await fresh.launch(request)
    with sqlite3.connect(root / "console-profiles.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM profile_studies").fetchone()[0] == 1
    assert list_studies(root) == []


def test_profile_api_has_no_login_and_explicit_attachment_requires_existing_journal(tmp_path):
    deps = bind_app_deps(tmp_path, catalog=minimal_test_catalog(), clock=FakeClock(),
                         stream_poll_interval_s=.01, stream_sleep=asyncio.sleep, run_wait_s=1)
    with TestClient(create_app(deps)) as client:
        assert client.get("/console/profiles").json() == {"profiles": []}
        profile = client.post("/console/profiles", json={"nickname": "User"})
        assert profile.status_code == 201
        pid = profile.json()["id"]
        assert client.post("/console/profiles", json={"nickname": "USER"}).status_code == 409
        assert client.post("/console/profiles", json={"nickname": " ", "password": "fake"}).status_code == 422
        assert client.get(f"/console/profiles/{pid}/studies").json() == []
        assert client.post(f"/console/profiles/{pid}/studies", json={"study_id": "missing"}).status_code == 404
        assert client.post(f"/console/profiles/{pid}/studies", json={"study_id": "../escape"}).status_code == 400
        assert client.get("/studies").json() == []


@pytest.mark.parametrize("action", ["accept", "cancel_with_unavailable_catalog"])
def test_profile_launch_keeps_existing_hitl_and_cancel_owner(tmp_path, monkeypatch, action):
    presets, _ = _presets(tmp_path)
    _patch_cli_surface(monkeypatch, _happy_bodies())
    root = tmp_path / "data"
    deps = bind_app_deps(root, catalog=minimal_test_catalog(), clock=FakeClock(),
                         stream_poll_interval_s=.001, stream_sleep=asyncio.sleep, run_wait_s=2)
    with TestClient(create_app(deps, run_presets=presets)) as client:
        profile = client.post("/console/profiles", json={"nickname": "Fixture"}).json()
        pid = profile["id"]
        request = {**_request().model_dump(), "profile_id": pid}
        launched = client.post("/console/runs", json=request)
        assert launched.status_code == 200, launched.text
        sid = launched.json()["study_id"]
        assert client.get(f"/console/profiles/{pid}/studies").json()[0]["study_id"] == sid
        for _ in range(100):
            rows = client.get(f"/console/studies/{sid}/hitl").json()
            if rows and rows[0]["can_answer"]:
                break
        assert rows and rows[0]["can_answer"]
        if action == "cancel_with_unavailable_catalog":
            (root / "console-profiles.sqlite3").write_bytes(b"broken catalog")
            assert client.get("/console/profiles").status_code == 503
            assert client.get(f"/console/runs/{sid}").json()["can_cancel"]
            assert client.post(f"/studies/{sid}/cancel", json={"by": "human"}).status_code == 200
            assert not client.get(f"/console/studies/{sid}/hitl").json()[0]["can_answer"]
        else:
            row = rows[0]
            answer = client.post(f"/studies/{sid}/hitl/{row['request_id']}", json={
                "decision": "accept", "opened_seq": row["opened_seq"],
                "context_sha256": row["prompt"]["context_sha256"],
            })
            assert answer.status_code == 200, answer.text
            for _ in range(100):
                status = client.get(f"/console/runs/{sid}").json()
                if status["run_status"] != "running":
                    break
            assert status["run_status"] == "done"
            assert client.post("/console/runs", json=request).json()["study_id"] == sid
