"""Frozen profile domain models (DESIGN §8). All fields required — no guessed defaults."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from coscientist.core.execution.domain import Budget
from coscientist.mcp.config import McpServerConfig

AlembicMode = Literal["enabled", "disabled"]


def _identifier(value: str) -> str:
    if not value or value != value.strip() or any(ch.isspace() for ch in value):
        raise ValueError(f"must be non-empty without whitespace: {value!r}")
    return value


Identifier = Annotated[str, AfterValidator(_identifier)]


class RoleConfig(BaseModel):
    """LLM role: model id + catalog tool names.

    ``prompt`` as ``PromptRef(prompt_id, version)`` waits on agent-layer #50 —
    not a ``str`` placeholder.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    model: Identifier
    tools: tuple[Identifier, ...]

    @model_validator(mode="after")
    def _unique_tools(self) -> RoleConfig:
        if len(self.tools) != len(set(self.tools)):
            raise ValueError("RoleConfig.tools names must be unique")
        return self


class RouteConfig(BaseModel):
    """Route name → role + ``Budget`` (DESIGN §7.4 / §8). No parallel budget model."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    role: Identifier
    budget: Budget


class Profile(BaseModel):
    """Versioned profile object (YAML loader lands in P3)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: Identifier
    profile_version: StrictInt = Field(ge=1)
    roles: dict[str, RoleConfig]
    routes: dict[str, RouteConfig]
    mcp_servers: tuple[McpServerConfig, ...]
    catalog_extensions: tuple[Identifier, ...]
    alembic: AlembicMode
    integrations: tuple[Identifier, ...]
    plugins: tuple[Identifier, ...]

    @field_validator("roles", "routes")
    @classmethod
    def _non_empty_identifier_keys(cls, v: dict[str, object]) -> dict[str, object]:
        if not v:
            raise ValueError("must be a non-empty mapping")
        for key in v:
            _identifier(key)
        return v

    @model_validator(mode="after")
    def _consistency(self) -> Profile:
        missing = sorted(
            {r.role for r in self.routes.values() if r.role not in self.roles}
        )
        if missing:
            raise ValueError(f"routes reference unknown roles: {missing}")
        names = [s.name for s in self.mcp_servers]
        if len(names) != len(set(names)):
            raise ValueError("mcp_servers names must be unique")
        return self
