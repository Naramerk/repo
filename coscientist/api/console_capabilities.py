"""Build browser-safe choices from pinned server presets, never client declarations."""
from __future__ import annotations

from collections.abc import Mapping

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
from coscientist.api.run import RunStudyBody, describe_run_capabilities
from coscientist.core.events.digest import canonical_json, sha256_hex


def configured_capabilities(presets: Mapping[str, PinnedPreset]) -> SettingsCatalog:
    """Expose configured models and trusted MCP bindings without URLs or credentials.

    Role IDs include the preset: CLI model flags confirm a packaged profile and
    are not arbitrary overrides. Tool IDs identify the actual server and tool.
    Conflicting declarations cannot be combined into one local-profile catalog.
    """
    roles: list[RoleChoices] = []
    servers: dict[str, ServerChoices] = {}
    bindings: dict[str, str] = {}
    role_choices: dict[str, str] = {}
    coder_available = False
    for preset_id, pinned in sorted(presets.items()):
        preset = pinned.preset
        settings = preset.config
        # Validate pinned resources even when merely opening the settings page.
        pinned.body("capability catalog validation")
        role_models, declared_servers = describe_run_capabilities(settings)
        active_roles = {"planner", "executor-route", "tz_spec", *settings.wired_roles}
        for role_id in sorted(active_roles):
            model = role_models.get(role_id)
            if model is None:
                raise PresetRefuse("preset_role_not_configured")
            key = f"{preset_id}:{role_id}"
            choice = sha256_hex(canonical_json({"preset": pinned.digest, "role": role_id, "model": model}))
            roles.append(RoleChoices(id=key, label=f"{preset.label} · {role_id}",
                                     choices=[ModelChoice(id=choice, label=model, model=model)]))
            role_choices[key] = choice
        coder_available = coder_available or settings.coder_backend is not None
        for name, (declaration, tool_names) in declared_servers.items():
            if name in bindings and bindings[name] != declaration:
                raise PresetRefuse("preset_capability_conflict")
            bindings[name] = declaration
            tools = {tool: ToolChoice(id=f"{name}:{tool}", label=tool) for tool in tool_names
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
        "bindings": dict(sorted(bindings.items())),
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
    _, declared_servers = describe_run_capabilities(preset.preset.config)
    available = {f"{name}:{tool}" for name, (_, tools) in declared_servers.items() for tool in tools}
    selected = available.intersection(values.enabled_tool_ids)
    if body.allowed_tool_ids is not None:
        selected.intersection_update(body.allowed_tool_ids)
    update: dict[str, object] = {"allowed_tool_ids": sorted(selected)}
    if not values.coder_enabled:
        update.update(coder_backend=None, coder_jobs_path=None)
    return body.model_copy(update=update)
