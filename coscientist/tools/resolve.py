"""Agent-facing ToolRouter resolve port (DESIGN §6.3).

MCP dispatch lives in ``coscientist.tools.router.ToolRouter`` (G7). Agents use
this Protocol + ``call_via_router`` so they never call ``Tool.call`` directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ToolSpecName(Protocol):
    """Minimal ``ToolSpec`` surface for AgentPort allow-lists (name only)."""

    name: str


class NamedTool(Protocol):
    """Allow-list tool entry for ``AgentPort.run`` (DESIGN §7.2 ``tools: list[Tool]``).

    Agents must not import ``tools.contract.Tool``; this Protocol is the resolve-port
    stand-in keyed by ``spec.name``.
    """

    spec: _ToolSpecName


class ToolNeed(BaseModel):
    """What the agent needs resolved before a tool call."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["by_name", "by_query"]
    value: str = Field(min_length=1)


class Resolution(BaseModel):
    """Result of ``ToolRouter.resolve`` (capability #8 statuses)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    status: Literal["tool_found", "tool_built", "coder", "tool_missing"]
    tool_name: str | None

    @model_validator(mode="after")
    def _status_matches_name(self) -> Self:
        if self.status == "tool_found":
            if self.tool_name is None or not self.tool_name:
                raise ValueError("tool_found requires non-empty tool_name")
        elif self.status == "tool_missing":
            if self.tool_name is not None:
                raise ValueError("tool_missing requires tool_name=None")
        return self


class ToolRouter(Protocol):
    """Resolve + call tools for the agent layer (DESIGN §6.3 statuses)."""

    def resolve(self, need: ToolNeed) -> Resolution: ...

    async def call(
        self,
        tool_name: str,
        args: Mapping[str, object],
        *,
        study_id: str,
        task_key: str,
        attempt_id: str,
    ) -> Mapping[str, object]: ...


async def call_via_router(
    router: ToolRouter,
    tool_name: str,
    args: Mapping[str, object],
    *,
    study_id: str,
    task_key: str,
    attempt_id: str,
) -> Mapping[str, object]:
    """Agent-facing ToolRouter entry (avoids ``Tool.call`` / bare ``.call`` in agents)."""
    return await router.call(
        tool_name,
        args,
        study_id=study_id,
        task_key=task_key,
        attempt_id=attempt_id,
    )
