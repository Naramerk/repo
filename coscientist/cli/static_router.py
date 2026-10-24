"""Explicit ToolRouter backed by a caller-supplied name→payload map (no MCP)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coscientist.tools.router import Resolution, ToolNeed


class StaticToolRouter:
    """Resolve/call only tools present in ``tools`` (G7 owns MCP wiring)."""

    def __init__(self, *, tools: Mapping[str, Mapping[str, Any]]) -> None:
        self._tools = dict(tools)

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
        del args, study_id, task_key, attempt_id
        if tool_name not in self._tools:
            raise KeyError(tool_name)
        return dict(self._tools[tool_name])
