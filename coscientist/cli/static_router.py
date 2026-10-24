"""Explicit ToolRouter backed by a caller-supplied name→payload map (no MCP)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coscientist.tools.router import Resolution, ToolNeed


class StaticToolRouter:
    """Resolve/call only tools present in ``tools`` (G7 owns MCP wiring).

    Static payloads are not parameterized: non-empty ``args`` are refused
    (no silent ``del args``).
    """

    def __init__(self, *, tools: Mapping[str, Mapping[str, Any]]) -> None:
        self._tools = dict(tools)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def resolve(self, need: ToolNeed) -> Resolution:
        if need.kind != "by_name":
            return Resolution(status="tool_missing", tool_name=None)
        if need.value in self._tools:
            return Resolution(status="tool_found", tool_name=need.value)
        return Resolution(status="tool_missing", tool_name=None)

    async def call(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        *,
        study_id: str,
        task_key: str,
        attempt_id: str,
    ) -> Mapping[str, Any]:
        if args:
            raise ValueError(
                f"StaticToolRouter refuses non-empty args for {tool_name!r} "
                f"(study_id={study_id!r} task_key={task_key!r} attempt_id={attempt_id!r}); "
                "static payloads are not parameterized"
            )
        if tool_name not in self._tools:
            raise KeyError(tool_name)
        return dict(self._tools[tool_name])
