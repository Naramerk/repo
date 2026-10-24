"""Agent wiring package (DESIGN §7 / M2).

Agents call tools only via ``coscientist.tools.invoke_tool``. Direct
``Tool.call``, ``register_tool``, and imports of ``tools.contract`` /
``core.tool_registry`` / ``core.dispose`` / ``core.artifact_stamp`` are
forbidden (import-linter + AST tests).

All agent state lives in the event journal (``LlmCallRecorded``, ``ToolCalled``,
``McpRequestRecorded`` / ``McpResponseRecorded``, ``UsageRecorded``,
``PlanExpansionRequested``). New subtasks only via ``AgentTaskOutput.expansion_request``.
"""

from __future__ import annotations

from coscientist.agents.config import (
    AgentBudgetLimits,
    AgentConfig,
    RoleConfig,
    agent_config_from_mapping,
    role_config_from_mapping,
)
from coscientist.agents.errors import (
    AgentBudgetExceeded,
    AgentConfigError,
    AgentDecisionError,
    AgentError,
    AgentReplayError,
    AgentRoleError,
)
from coscientist.agents.journal import (
    digest_of,
    put_json_artifact,
    record_llm_call,
    record_mcp_request,
    record_mcp_response,
    record_plan_expansion_requested,
    record_tool_called,
    record_usage,
)
from coscientist.agents.port import JournaledAgentPort, llm_calls_from_events
from coscientist.agents.types import (
    AgentDecision,
    AgentRun,
    AggregateUsage,
    ToolCallRequest,
)

__all__ = [
    "AgentBudgetExceeded",
    "AgentBudgetLimits",
    "AgentConfig",
    "AgentConfigError",
    "AgentDecision",
    "AgentDecisionError",
    "AgentError",
    "AgentReplayError",
    "AgentRoleError",
    "AgentRun",
    "AggregateUsage",
    "JournaledAgentPort",
    "RoleConfig",
    "ToolCallRequest",
    "agent_config_from_mapping",
    "digest_of",
    "llm_calls_from_events",
    "put_json_artifact",
    "record_llm_call",
    "record_mcp_request",
    "record_mcp_response",
    "record_plan_expansion_requested",
    "record_tool_called",
    "record_usage",
    "role_config_from_mapping",
]
