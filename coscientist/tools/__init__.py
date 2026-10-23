"""Tool contracts, side-effect gate, effect idempotency, and taint (DESIGN_M1 §8)."""

from coscientist.tools.contract import (
    CallCtx,
    Tool,
    ToolEffect,
    ToolError,
    ToolOutput,
    ToolSpec,
)
from coscientist.tools.effects import (
    effect_idem_key,
    invoke_tool,
    load_effect_index,
)
from coscientist.tools.gate import side_effect_allowed, side_effect_decision
from coscientist.tools.taint import (
    context_is_tainted,
    filter_tools_for_context,
)

__all__ = [
    "CallCtx",
    "Tool",
    "ToolEffect",
    "ToolError",
    "ToolOutput",
    "ToolSpec",
    "context_is_tainted",
    "effect_idem_key",
    "filter_tools_for_context",
    "invoke_tool",
    "load_effect_index",
    "side_effect_allowed",
    "side_effect_decision",
]
