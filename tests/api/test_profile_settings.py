from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coscientist.api.local_profiles import LocalProfiles, ProfileRefuse
from coscientist.api.profile_settings import ProfileSettings, SettingsCatalog, SettingsValues
from coscientist.api.profile_settings_routes import register_profile_settings_routes


def catalog() -> SettingsCatalog:
    return SettingsCatalog.model_validate({
        "digest": "a" * 64,
        "roles": [{"id": "planner", "label": "Planner", "choices": [
            {"id": "trusted-a", "label": "Configured A", "model": "fixture-a"},
            {"id": "trusted-b", "label": "Configured B", "model": "fixture-b"}]}],
        "mcp_servers": [{"id": "server", "label": "Trusted server", "tools": [
            {"id": "server:search", "label": "Search"}]}],
        "coder": {"available": True, "tool_ids": ["coder:run"]}, "preset_ids": ["configured"],
        "defaults": {"role_choices": {"planner": "trusted-a"}, "enabled_server_ids": ["server"],
                     "enabled_tool_ids": ["server:search"], "coder_enabled": False},
    })


def test_settings_revisions_survive_restart_and_migrate_v1_without_assigning_defaults(tmp_path: Path) -> None:
    profiles = LocalProfiles(tmp_path)
    profile = profiles.create("Fixture")
    pid = profile["id"]
    with profiles._connection(write=True) as connection:
        assert connection is not None
        connection.execute("DROP TABLE profile_settings")
        connection.execute("PRAGMA user_version=1")
    store = ProfileSettings(profiles)
    assert store.get(pid) == {"revision": 0, "settings": None, "catalog_digest": None, "settings_digest": None}
    first = store.save(pid, expected_revision=0, catalog_digest=catalog().digest,
                       settings=catalog().defaults, catalog=catalog())
    changed = catalog().defaults.model_copy(update={"role_choices": {"planner": "trusted-b"}})
    second = store.save(pid, expected_revision=1, catalog_digest=catalog().digest, settings=changed, catalog=catalog())
    restored = ProfileSettings(LocalProfiles(tmp_path))
    assert restored.get(pid) == second
    assert restored.get(pid, 1) == first
    assert restored.get(pid, 0)["settings"] is None
    assert first["settings_digest"] != second["settings_digest"]
    with pytest.raises(ProfileRefuse, match="settings_revision_not_found"):
        restored.get(pid, 99)


def test_concurrent_saves_use_one_optimistic_revision(tmp_path: Path) -> None:
    profiles = LocalProfiles(tmp_path)
    pid = profiles.create("Fixture")["id"]
    def save() -> int:
        try:
            ProfileSettings(profiles).save(pid, expected_revision=0, catalog_digest=catalog().digest,
                                           settings=catalog().defaults, catalog=catalog())
            return 200
        except ProfileRefuse as exc:
            return exc.status
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: save(), range(4)))
    assert sorted(outcomes) == [200, 409, 409, 409]
    assert ProfileSettings(profiles).get(pid)["revision"] == 1


@pytest.mark.parametrize("change", [
    {"role_choices": {"planner": "untrusted-model"}},
    {"role_choices": {"extra": "trusted-a"}},
    {"enabled_server_ids": ["unknown"]},
    {"enabled_server_ids": [], "enabled_tool_ids": ["server:search"]},
    {"enabled_tool_ids": ["coder:run"], "coder_enabled": False},
    {"enabled_tool_ids": ["server:search", "server:search"]},
])
def test_settings_refuse_unknown_and_disabled_choices(tmp_path: Path, change: dict[str, object]) -> None:
    profiles = LocalProfiles(tmp_path)
    pid = profiles.create("Fixture")["id"]
    values = SettingsValues.model_validate({**catalog().defaults.model_dump(), **change})
    store = ProfileSettings(profiles)
    with pytest.raises(ProfileRefuse):
        store.save(pid, expected_revision=0, catalog_digest=catalog().digest, settings=values, catalog=catalog())
    assert store.get(pid)["revision"] == 0


def test_settings_routes_require_real_catalog_and_refuse_stale_or_corrupted_revisions(tmp_path: Path) -> None:
    profiles = LocalProfiles(tmp_path)
    pid = profiles.create("Fixture")["id"]
    app = FastAPI()
    register_profile_settings_routes(app, profiles=profiles, catalog=catalog)
    client = TestClient(app)
    path = f"/console/profiles/{pid}/settings"
    assert client.get("/console/settings-catalog").json()["defaults"] == catalog().defaults.model_dump()
    body = {"expected_revision": 0, "catalog_digest": catalog().digest, "settings": catalog().defaults.model_dump()}
    assert client.put(path, json={**body, "password": "not-auth"}).status_code == 422
    assert client.put(path, json={**body, "catalog_digest": "b" * 64}).status_code == 409
    assert client.put(path, json=body).json()["revision"] == 1
    assert client.put(path, json=body).status_code == 409
    with profiles._connection(write=True) as connection:
        assert connection is not None
        connection.execute("UPDATE profile_settings SET settings_digest=? WHERE profile_id=?", ("0" * 64, pid))
    assert client.get(path).status_code == 503
    unavailable = FastAPI()
    register_profile_settings_routes(unavailable, profiles=profiles, catalog=None)
    assert TestClient(unavailable).get("/console/settings-catalog").status_code == 503
    assert TestClient(unavailable).put(path, json=body).status_code == 503


def test_catalog_refuses_duplicate_tool_identity_between_servers_and_coder() -> None:
    raw = catalog().model_dump()
    raw["coder"] = {"available": True, "tool_ids": ["server:search"]}
    with pytest.raises(ValueError, match="unique"):
        SettingsCatalog.model_validate(raw)
