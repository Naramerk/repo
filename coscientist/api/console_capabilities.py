"""Build browser-safe choices from pinned server presets, never client declarations."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coscientist.api.presets import PinnedPreset, PresetRefuse
from coscientist.api.run import describe_run_capabilities
from coscientist.core.events.digest import canonical_json, sha256_hex


class _CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ModelChoice(_CatalogModel):
    id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1)
    model: str = Field(min_length=1)


class RoleChoices(_CatalogModel):
    id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1)
    choices: list[ModelChoice]


class ToolChoice(_CatalogModel):
    id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1)


class ServerChoices(_CatalogModel):
    id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1)
    tools: list[ToolChoice]


class CoderChoices(_CatalogModel):
    available: bool
    tool_ids: list[str]  # coder tools are not MCP bindings; reserved for a future trusted source


class CapabilityCatalog(_CatalogModel):
    """Browser-safe capability view of configured presets: models, tool names, digests."""

    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    roles: list[RoleChoices]
    mcp_servers: list[ServerChoices]
    coder: CoderChoices
    preset_ids: list[str]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        groups = [[role.id for role in self.roles], [server.id for server in self.mcp_servers],
                  [tool.id for server in self.mcp_servers for tool in server.tools] + self.coder.tool_ids,
                  self.preset_ids]
        groups.extend([choice.id for choice in role.choices] for role in self.roles)
        if any(len(group) != len(set(group)) for group in groups):
            raise ValueError("capability catalog IDs must be unique within their namespace")
        return self


def configured_capabilities(presets: Mapping[str, PinnedPreset]) -> CapabilityCatalog:
    """Expose configured models and trusted MCP bindings without URLs or credentials.

    Role IDs include the preset: CLI model flags confirm a packaged profile and
    are not arbitrary overrides. Tool IDs identify the actual server and tool,
    already narrowed by the preset's ``allowed_tool_ids`` mask when it pins one.
    Conflicting declarations cannot be combined into one catalog.
    """
    roles: list[RoleChoices] = []
    servers: dict[str, ServerChoices] = {}
    bindings: dict[str, str] = {}
    coder_available = False
    for preset_id, pinned in sorted(presets.items()):
        preset = pinned.preset
        settings = preset.config
        # Validate pinned resources even when merely describing capabilities.
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
    digest = sha256_hex(canonical_json({
        "presets": {key: value.digest for key, value in sorted(presets.items())},
        "roles": [role.model_dump(mode="json") for role in roles],
        "bindings": dict(sorted(bindings.items())),
    }))
    return CapabilityCatalog(
        digest=digest, roles=roles, mcp_servers=[servers[name] for name in sorted(servers)],
        coder=CoderChoices(available=coder_available, tool_ids=[]), preset_ids=sorted(presets),
    )
