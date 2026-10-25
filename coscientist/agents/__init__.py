"""Agent wiring package (DESIGN §7 / §13 D).

Agents call tools via ``self._router.call`` (MCP ``ToolRouter`` = G7) or
``invoke_tool``. ``CallCtx`` is imported from ``tools.call_ctx`` (not
``tools.contract``); ``Tool`` / star-imports of contract, ``register_tool``, and
``tool_registry`` are forbidden (AST + import-linter).
"""

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.schemas import (
    AgentError,
    AgentRun,
    ExecutorRouteIn,
    ExecutorRouteOut,
    PlanOut,
    StudyContext,
    TaskSpecDraft,
    ToolCallRef,
)

__all__ = [
    "AgentError",
    "AgentRun",
    "ExecutorRouteIn",
    "ExecutorRouteOut",
    "LlmAgentAdapter",
    "PlanOut",
    "StudyContext",
    "TaskSpecDraft",
    "ToolCallRef",
]
