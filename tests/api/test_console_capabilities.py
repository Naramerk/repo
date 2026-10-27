"""Trusted catalog and revision-pinned launch authorization; no external calls."""
from __future__ import annotations

import json

import pytest

from coscientist.api.console_capabilities import apply_profile_settings, configured_capabilities
from coscientist.api.console_runs import ConsoleRunService, LaunchBody
from coscientist.api.local_profiles import ProfileRefuse
from coscientist.api.presets import PresetRefuse, load_run_presets
from coscientist.api.profile_settings import ProfileSettings
from coscientist.api.run import RunStudyBody, _namespace_from_body, _RunRecord
from coscientist.core.events.digest import canonical_json, sha256_hex
from tests.api.test_console_runs import _presets
from tests.api.test_run_endpoint import _run_body, _service
from tests.tools.test_mcp_live import _bundle


def _configured(tmp_path):
    presets, document = _presets(tmp_path)
    bundle = tmp_path / "trusted-bundle.json"
    bundle.write_text(_bundle().model_dump_json(), encoding="utf-8")
    document["presets"][0]["config"]["mcp_tool_bundle_path"] = str(bundle)
    path = tmp_path / "presets.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_run_presets(path)


def test_historical_body_and_preset_digests_preserved(tmp_path):
    raw = _run_body(tmp_path)
    body = RunStudyBody.model_validate(raw)
    assert body.model_dump(mode="json") == raw
    assert "allowed_tool_ids" not in json.loads(body.model_dump_json())
    deny = body.model_copy(update={"allowed_tool_ids": []})
    assert deny.model_dump(mode="json")["allowed_tool_ids"] == []
    ns = _namespace_from_body(deny, sid="study", data_root=tmp_path, task=tmp_path / "task.txt")
    assert ns.allowed_tool_ids == []
    presets, document = _presets(tmp_path)
    pinned = presets["research"]
    assert pinned.digest == sha256_hex(canonical_json({
        "preset": document["presets"][0], "references": pinned.reference_hashes,
    }))


def test_catalog_exposes_real_binding_without_transport_or_credentials(tmp_path):
    presets = _configured(tmp_path)
    catalog = configured_capabilities(presets)
    assert catalog.defaults.enabled_tool_ids == ["papers-search:echo"]
    assert catalog.roles and all(len(role.choices) == 1 for role in catalog.roles)
    public = catalog.model_dump_json()
    assert str(tmp_path) not in public and "command" not in public and "api_key_env" not in public
    changed = tmp_path / "trusted-bundle.json"
    changed.write_text("{}", encoding="utf-8")
    with pytest.raises(PresetRefuse, match="configuration_changed"):
        configured_capabilities(presets)


def test_settings_can_only_narrow_existing_preset(tmp_path):
    presets = _configured(tmp_path)
    preset = presets["research"]
    catalog = configured_capabilities(presets)
    body = preset.body("Task")
    values = catalog.defaults.model_copy(update={"enabled_tool_ids": [], "enabled_server_ids": []})
    assert apply_profile_settings(body, values=values, catalog=catalog, preset=preset).allowed_tool_ids == []
    assert apply_profile_settings(body.model_copy(update={"allowed_tool_ids": []}),
                                  values=catalog.defaults, catalog=catalog, preset=preset).allowed_tool_ids == []
    unknown = catalog.defaults.model_copy(update={"enabled_tool_ids": ["untrusted:tool"]})
    with pytest.raises(ProfileRefuse, match="unavailable"):
        apply_profile_settings(body, values=unknown, catalog=catalog, preset=preset)


def test_conflicting_server_declarations_refused(tmp_path):
    presets = _configured(tmp_path)
    pinned = presets["research"]
    alternate = tmp_path / "alternate.json"
    original = _bundle()
    server = original.client.servers[0].model_copy(update={"command": "/different/interpreter"})
    bundle = original.model_copy(update={"client": original.client.model_copy(update={"servers": (server,)})})
    alternate.write_text(bundle.model_dump_json(), encoding="utf-8")
    config = pinned.preset.config.model_copy(update={"mcp_tool_bundle_path": str(alternate)})
    # Load normally to obtain independently pinned resources and identity.
    path = tmp_path / "other-presets.json"
    other_preset = pinned.preset.model_copy(update={"id": "other", "config": config})
    path.write_text(json.dumps({"presets": [other_preset.model_dump(mode="json")]}))
    other = load_run_presets(path)["other"]
    with pytest.raises(PresetRefuse, match="capability_conflict"):
        configured_capabilities({**presets, "other": other})


@pytest.mark.asyncio
async def test_launch_pins_saved_revision_and_never_uses_latest_implicitly(tmp_path, monkeypatch):
    presets = _configured(tmp_path)
    root = tmp_path / "data"
    runs = _service(root)
    service = ConsoleRunService(root, runs=runs, presets=presets, wait_s=1)
    profile = service.profiles.create("Researcher")
    catalog = service.settings_catalog()
    store = ProfileSettings(service.profiles)
    denied = catalog.defaults.model_copy(update={"enabled_tool_ids": []})
    store.save(profile["id"], expected_revision=0, catalog_digest=catalog.digest, settings=denied, catalog=catalog)
    seen = []

    async def start(sid, body):
        seen.append(body)
        runs._runs[sid] = _RunRecord(sid, "running", None, None, None, None, None)

    monkeypatch.setattr(runs, "post_run", start)
    request = LaunchBody(preset_id="research", task_text="Task", idempotency_key="pinned",
                         profile_id=profile["id"], profile_settings_revision=1)
    await service.launch(request)
    assert seen[0].allowed_tool_ids == []
    store.save(profile["id"], expected_revision=1, catalog_digest=catalog.digest,
               settings=catalog.defaults, catalog=catalog)
    await service.launch(request)
    assert len(seen) == 1
    await service.launch(request.model_copy(update={"idempotency_key": "new", "profile_settings_revision": 2}))
    assert seen[1].allowed_tool_ids == ["papers-search:echo"]
    reservation = next((root / "idempotency" / "console-runs").iterdir())
    context = json.loads(reservation.read_text())["context"]
    assert "profile_settings" in context
