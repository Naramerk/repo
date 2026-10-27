"""Build browser-safe choices from pinned server presets, never client declarations."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from coscientist.api.presets import PinnedPreset, PresetRefuse
from coscientist.api.profile_settings import (
    CoderChoices,
    ModelChoice,
    RoleChoices,
    ServerChoices,
    SettingsCatalog,
    SettingsValues,
    ToolChoice,
    validate_settings,
)
from coscientist.api.run import RunStudyBody
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.profiles.load import load_profile
from coscientist.tools.mcp_live import McpToolRouterBundle


def configured_capabilities(presets: Mapping[str, PinnedPreset]) -> SettingsCatalog:
    """Expose configured models and trusted MCP bindings without URLs or credentials.

    Role IDs include the preset: CLI model flags confirm a packaged profile and
    are not arbitrary overrides. Tool IDs identify the actual server and tool.
    Conflicting declarations cannot be combined into one local-profile catalog.
    """
    roles: list[RoleChoices] = []
    servers: dict[str, ServerChoices] = {}
    bindings: dict[str, bytes] = {}
    role_choices: dict[str, str] = {}
    coder_available = False
    for preset_id, pinned in sorted(presets.items()):
        preset = pinned.preset
        settings = preset.config
        # Validate pinned resources even when merely opening the settings page.
        pinned.body("capability catalog validation")
        profile = load_profile(settings.profile, expected_tool_catalog_version=settings.tool_catalog_version)
        active_roles = {"planner", "executor-route", "tz_spec", *settings.wired_roles}
        for role_id in sorted(active_roles):
            role = profile.roles.get(role_id)
            if role is None:
                raise PresetRefuse("preset_role_not_configured")
            key = f"{preset_id}:{role_id}"
            choice = sha256_hex(canonical_json({"preset": pinned.digest, "role": role_id, "model": role.model}))
            roles.append(RoleChoices(id=key, label=f"{preset.label} · {role_id}",
                                     choices=[ModelChoice(id=choice, label=role.model, model=role.model)]))
            role_choices[key] = choice
        coder_available = coder_available or settings.coder_backend is not None
        if settings.mcp_tool_bundle_path is None:
            continue
        bundle = McpToolRouterBundle.model_validate_json(
            Path(settings.mcp_tool_bundle_path).read_bytes())
        connections = {server.name: server for server in bundle.client.servers}
        for server in bundle.router.servers:
            name = server.server_name
            declaration = canonical_json({"connection": connections[name].model_dump(mode="json"),
                                          "binding": server.model_dump(mode="json")})
            if name in bindings and bindings[name] != declaration:
                raise PresetRefuse("preset_capability_conflict")
            bindings[name] = declaration
            tools = {tool: ToolChoice(id=f"{name}:{tool}", label=tool) for tool in server.tools
                     if settings.allowed_tool_ids is None or f"{name}:{tool}" in settings.allowed_tool_ids}
            if name in servers:
                tools.update({tool.label: tool for tool in servers[name].tools})
            servers[name] = ServerChoices(
                id=name, label=name,
                tools=[tools[tool] for tool in sorted(tools)],
            )
    tool_ids = [tool.id for server in servers.values() for tool in server.tools]
    digest = sha256_hex(canonical_json({
        "presets": {key: value.digest for key, value in sorted(presets.items())},
        "roles": [role.model_dump(mode="json") for role in roles],
        "bindings": {name: sha256_hex(value) for name, value in sorted(bindings.items())},
    }))
    return SettingsCatalog(
        digest=digest, roles=roles, mcp_servers=[servers[name] for name in sorted(servers)],
        coder=CoderChoices(available=coder_available, tool_ids=[]), preset_ids=sorted(presets),
        defaults=SettingsValues(role_choices=role_choices, enabled_server_ids=sorted(servers),
                                enabled_tool_ids=sorted(tool_ids), coder_enabled=coder_available),
    )


def apply_profile_settings(body: RunStudyBody, *, values: SettingsValues,
                           catalog: SettingsCatalog, preset: PinnedPreset) -> RunStudyBody:
    """A local profile can narrow a trusted preset, never grant new tools/models."""
    validate_settings(values, catalog)
    available: set[str] = set()
    path = preset.preset.config.mcp_tool_bundle_path
    if path is not None:
        bundle = McpToolRouterBundle.model_validate_json(Path(path).read_bytes())
        available = {f"{server.server_name}:{tool}" for server in bundle.router.servers for tool in server.tools}
    selected = available.intersection(values.enabled_tool_ids)
    if body.allowed_tool_ids is not None:
        selected.intersection_update(body.allowed_tool_ids)
    update: dict[str, object] = {"allowed_tool_ids": sorted(selected)}
    if not values.coder_enabled:
        update.update(coder_backend=None, coder_jobs_path=None)
    return body.model_copy(update=update)
