"""Frozen profile models (DESIGN §8). Budget=core; P3 waits on N1 (#70); do not wrap."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from coscientist.core.execution.domain import Budget
from coscientist.mcp.config import Identifier, McpServers

AlembicMode = Literal["enabled", "disabled"]


class RoleConfig(BaseModel):
    """LLM role: model + catalog tools. ``prompt: PromptRef`` waits on #50."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    model: Identifier
    tools: tuple[Identifier, ...]

    @model_validator(mode="after")
    def _unique_tools(self) -> RoleConfig:
        if len(self.tools) != len(set(self.tools)):
            raise ValueError("RoleConfig.tools names must be unique")
        return self


class RouteConfig(BaseModel):
    """Route → role + core ``Budget`` (§7.4 / §8)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    role: Identifier
    budget: Budget


class Profile(BaseModel):
    """Versioned profile (YAML loader = P3)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: Identifier
    profile_version: StrictInt = Field(ge=1)
    roles: dict[Identifier, RoleConfig] = Field(min_length=1)
    routes: dict[Identifier, RouteConfig] = Field(min_length=1)
    mcp_servers: McpServers
    catalog_extensions: tuple[Identifier, ...]
    alembic: AlembicMode
    integrations: tuple[Identifier, ...]
    plugins: tuple[Identifier, ...]

    @model_validator(mode="after")
    def _consistency(self) -> Profile:
        missing = sorted(
            {r.role for r in self.routes.values() if r.role not in self.roles}
        )
        if missing:
            raise ValueError(f"routes reference unknown roles: {missing}")
        return self
