"""Agent wiring package (DESIGN §7 / §13 D).

Agents call tools via ``coscientist.tools.router.ToolRouter.call`` (MCP impl = G7)
or ``coscientist.tools.invoke_tool``. Direct ``Tool.call``, ``register_tool``, and
imports of ``tools.contract`` / ``core.tool_registry`` are forbidden.
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
