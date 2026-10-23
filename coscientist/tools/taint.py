"""Taint helpers + tool filtering using the closed core registry (DESIGN_M1 §8)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from coscientist.core.execution.taint import artifact_is_tainted
from coscientist.core.execution.tool_registry import resolve_effect
from coscientist.tools.contract import Tool, ToolSpec


def context_is_tainted(artifacts: Iterable[object]) -> bool:
    """True when any artifact meta carries ``tainted=True``."""
    return any(artifact_is_tainted(a) for a in artifacts)


def _tool_name(tool: Tool | ToolSpec) -> str:
    return tool.name if isinstance(tool, ToolSpec) else tool.spec.name


def filter_tools_for_context(
    tools: Sequence[Tool | ToolSpec],
    *,
    tainted_context: bool,
) -> list[Tool | ToolSpec]:
    """Drop write/external_effect tools when context is tainted (registry effect)."""
    if not tainted_context:
        return list(tools)
    return [t for t in tools if resolve_effect(_tool_name(t)) == "read"]
