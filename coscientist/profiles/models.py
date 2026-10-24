"""Frozen profile domain models (DESIGN §8). All fields required — no guessed defaults."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from coscientist.mcp.config import StdioServerConfig, StreamableHttpServerConfig

AlembicMode = Literal["enabled", "disabled"]


class ProfileStdioServerConfig(StdioServerConfig):
    """Strict stdio MCP server entry for profiles (no str→number / list→tuple coerce)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ProfileHttpServerConfig(StreamableHttpServerConfig):
    """Strict streamable-HTTP MCP server entry for profiles."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


ProfileMcpServerConfig = Annotated[
    ProfileStdioServerConfig | ProfileHttpServerConfig,
    Field(discriminator="transport"),
]


class RoleConfig(BaseModel):
    """LLM role: model id, prompt ref, tool names from the MCP catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    model: str = Field(min_length=1)
    prompt_ref: str = Field(min_length=1)
    tools: tuple[str, ...]

    @field_validator("model", "prompt_ref")
    @classmethod
    def _strip_ok(cls, v: str) -> str:
        if v != v.strip():
            raise ValueError("must not have leading/trailing whitespace")
        return v

    @field_validator("tools")
    @classmethod
    def _tool_names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for name in v:
            if not name or name != name.strip() or any(ch.isspace() for ch in name):
                raise ValueError(f"tool name must be non-empty without whitespace: {name!r}")
        return v


class RouteBudget(BaseModel):
    """Per-route budget caps (profile-owned)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    max_llm_calls: int = Field(ge=0)
    max_tokens: int = Field(ge=0)
    max_tool_calls: int = Field(ge=0)
    timeout_s: float = Field(gt=0)


class RouteConfig(BaseModel):
    """Route name → role + budget (DESIGN §8)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    role: str = Field(min_length=1)
    budget: RouteBudget

    @field_validator("role")
    @classmethod
    def _role_name(cls, v: str) -> str:
        if v != v.strip() or any(ch.isspace() for ch in v):
            raise ValueError(f"role must be non-empty without whitespace: {v!r}")
        return v


class Profile(BaseModel):
    """Versioned profile object (YAML loader lands in G3)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str = Field(min_length=1)
    profile_version: StrictInt = Field(ge=1)
    roles: dict[str, RoleConfig]
    routes: dict[str, RouteConfig]
    mcp_servers: tuple[ProfileMcpServerConfig, ...]
    catalog_extensions: tuple[str, ...]
    alembic: AlembicMode
    integrations: tuple[str, ...]
    plugins: tuple[str, ...]

    @field_validator("name")
    @classmethod
    def _strip_ok(cls, v: str) -> str:
        if v != v.strip():
            raise ValueError("must not have leading/trailing whitespace")
        return v

    @field_validator("roles", "routes")
    @classmethod
    def _non_empty_keys(cls, v: dict[str, object]) -> dict[str, object]:
        if not v:
            raise ValueError("must be a non-empty mapping")
        for key in v:
            if not key or key != key.strip() or any(ch.isspace() for ch in key):
                raise ValueError(f"key must be non-empty without whitespace: {key!r}")
        return v

    @field_validator("catalog_extensions", "integrations", "plugins")
    @classmethod
    def _id_tuples(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for item in v:
            if not item or item != item.strip() or any(ch.isspace() for ch in item):
                raise ValueError(f"id must be non-empty without whitespace: {item!r}")
        return v

    @model_validator(mode="after")
    def _routes_reference_roles(self) -> Profile:
        missing = sorted(
            {r.role for r in self.routes.values() if r.role not in self.roles}
        )
        if missing:
            raise ValueError(f"routes reference unknown roles: {missing}")
        return self
