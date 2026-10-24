"""Config for optional A2A / MCP facades. Every operational field is required."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class A2AAgentCardConfig(BaseModel):
    """A2A agent card — all identity fields from config (never invented in code)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    url: str = Field(min_length=1)
    version: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    default_input_modes: tuple[str, ...]
    default_output_modes: tuple[str, ...]
    skills: tuple[str, ...]

    @field_validator(
        "default_input_modes",
        "default_output_modes",
        "skills",
        mode="before",
    )
    @classmethod
    def _list_to_tuple(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v

    @field_validator("name", "description", "url", "version", "protocol_version")
    @classmethod
    def _stripped_nonempty(cls, v: str) -> str:
        if v != v.strip() or v == "":
            raise ValueError("must be a non-empty stripped string")
        return v

    @field_validator("default_input_modes", "default_output_modes", "skills")
    @classmethod
    def _nonempty_tokens(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("tuple must be non-empty")
        for item in v:
            if not isinstance(item, str) or item != item.strip() or item == "":
                raise ValueError("entries must be non-empty stripped strings")
        return v


class ExternalMasConfig(BaseModel):
    """Which optional integrations are enabled (DESIGN §6.5 ``integrations: [...]``)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    enabled_integrations: tuple[str, ...]

    @field_validator("enabled_integrations", mode="before")
    @classmethod
    def _list_to_tuple(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v

    @field_validator("enabled_integrations")
    @classmethod
    def _names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for name in v:
            if not isinstance(name, str) or name != name.strip() or name == "":
                raise ValueError("integration names must be non-empty stripped strings")
            if name in seen:
                raise ValueError(f"duplicate integration name: {name!r}")
            seen.add(name)
        return v

    def requires(self, name: str) -> None:
        """Raise ``IntegrationDisabled`` when ``name`` is not enabled."""
        from coscientist.integrations.errors import IntegrationDisabled

        if name not in self.enabled_integrations:
            raise IntegrationDisabled(name)


class A2AFacadeConfig(BaseModel):
    """Inbound A2A facade config (agent card + study-create defaults from profile)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    mas: ExternalMasConfig
    agent_card: A2AAgentCardConfig
    # Profile / catalog for studies created via A2A tasks — from config only.
    study_profile: str = Field(min_length=1)
    catalog_version: int = Field(ge=0)

    @field_validator("study_profile")
    @classmethod
    def _profile(cls, v: str) -> str:
        if v != v.strip() or v == "":
            raise ValueError("study_profile must be a non-empty stripped string")
        return v

    @field_validator("catalog_version")
    @classmethod
    def _strict_catalog(cls, v: int) -> int:
        if isinstance(v, bool) or type(v) is not int:
            raise ValueError("catalog_version must be a strict int")
        return v


class A2ARemoteToolConfig(BaseModel):
    """Outbound A2A: remote agent callable as a Tool (provider=http)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    mas: ExternalMasConfig
    tool_name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    create_task_path: str = Field(min_length=1)
    timeout_s: float
    ok_status_codes: tuple[int, ...]
    request_content_type: str = Field(min_length=1)
    # JSON object key under which the tool places the agent prompt / request text.
    message_field: str = Field(min_length=1)
    # JSON object key where the peer returns the result payload.
    result_field: str = Field(min_length=1)

    @field_validator("ok_status_codes", mode="before")
    @classmethod
    def _list_to_tuple(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v

    @field_validator("tool_name", "base_url", "create_task_path", "message_field", "result_field")
    @classmethod
    def _stripped(cls, v: str) -> str:
        if v != v.strip() or v == "":
            raise ValueError("must be a non-empty stripped string")
        return v

    @field_validator("base_url")
    @classmethod
    def _url_no_ws(cls, v: str) -> str:
        if any(ch.isspace() for ch in v):
            raise ValueError("base_url must not contain whitespace")
        return v

    @field_validator("create_task_path")
    @classmethod
    def _relative_path(cls, v: str) -> str:
        if v.startswith("/"):
            raise ValueError("create_task_path must be relative (no leading '/')")
        if any(ch.isspace() for ch in v):
            raise ValueError("create_task_path must not contain whitespace")
        return v

    @field_validator("timeout_s")
    @classmethod
    def _positive_finite(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0.0:
            raise ValueError("timeout_s must be finite and > 0")
        return v

    @field_validator("ok_status_codes")
    @classmethod
    def _statuses(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if not v:
            raise ValueError("ok_status_codes must be non-empty")
        for code in v:
            if isinstance(code, bool) or type(code) is not int or not (100 <= code <= 599):
                raise ValueError(f"ok_status_codes entry out of range: {code!r}")
        if len(v) != len(set(v)):
            raise ValueError("ok_status_codes must not contain duplicates")
        return v

    @field_validator("request_content_type")
    @classmethod
    def _content_type(cls, v: str) -> str:
        if v != v.strip() or v == "" or any(ch.isspace() for ch in v):
            raise ValueError("request_content_type must be a non-empty token without whitespace")
        return v


class McpPublishedModule(BaseModel):
    """One module published as an MCP tool (name + schema from config)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, Any]

    @field_validator("name", "description")
    @classmethod
    def _stripped(cls, v: str) -> str:
        if v != v.strip() or v == "":
            raise ValueError("must be a non-empty stripped string")
        return v


class McpServerFacadeConfig(BaseModel):
    """MCP server facade: publish configured modules that call injected handlers."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    mas: ExternalMasConfig
    protocol_version: str = Field(min_length=1)
    supported_protocol_versions: tuple[str, ...]
    server_name: str = Field(min_length=1)
    server_version: str = Field(min_length=1)
    modules: tuple[McpPublishedModule, ...]

    @field_validator("supported_protocol_versions", "modules", mode="before")
    @classmethod
    def _list_to_tuple(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v

    @field_validator("protocol_version", "server_name", "server_version")
    @classmethod
    def _stripped(cls, v: str) -> str:
        if v != v.strip() or v == "":
            raise ValueError("must be a non-empty stripped string")
        return v

    @field_validator("supported_protocol_versions")
    @classmethod
    def _versions(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("supported_protocol_versions must be non-empty")
        for ver in v:
            if not isinstance(ver, str) or ver == "" or any(ch.isspace() for ch in ver):
                raise ValueError("protocol versions must be non-empty without whitespace")
        return v

    @field_validator("modules")
    @classmethod
    def _modules(cls, v: tuple[McpPublishedModule, ...]) -> tuple[McpPublishedModule, ...]:
        if not v:
            raise ValueError("modules must be non-empty")
        names = [m.name for m in v]
        if len(names) != len(set(names)):
            raise ValueError("module names must be unique")
        return v

    @model_validator(mode="after")
    def _cross(self) -> McpServerFacadeConfig:
        if self.protocol_version not in self.supported_protocol_versions:
            raise ValueError("protocol_version must be listed in supported_protocol_versions")
        return self
