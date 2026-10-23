"""Taint marking and tool filtering (DESIGN_M1 §8 / K5)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from coscientist.tools.contract import Tool, ToolEffect, ToolSpec


def context_is_tainted(
    artifacts: Iterable[Any],
) -> bool:
    """True when any artifact in the attempt context carries ``tainted=True``."""
    for art in artifacts:
        if art is None:
            continue
        if getattr(art, "tainted", False):
            return True
        if isinstance(art, dict) and art.get("tainted"):
            return True
    return False


def _effect_of(tool: Tool | ToolSpec) -> ToolEffect:
    spec = tool if isinstance(tool, ToolSpec) else tool.spec
    return spec.effect


def filter_tools_for_context(
    tools: Sequence[Tool | ToolSpec],
    *,
    tainted_context: bool,
) -> list[Tool | ToolSpec]:
    """Drop write/external_effect tools when the attempt context is tainted."""
    if not tainted_context:
        return list(tools)
    return [t for t in tools if _effect_of(t) == "read"]
