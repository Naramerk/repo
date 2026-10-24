"""Typed agent-layer failures (no silent fallbacks)."""

from __future__ import annotations


class AgentError(Exception):
    """Base for agent-layer typed refusals."""


class AgentConfigError(AgentError):
    """Missing or invalid agent/profile configuration."""


class AgentBudgetExceeded(AgentError):
    """Attempt/role budget ceiling reached before or during a call."""

    def __init__(self, scope: str, detail: str) -> None:
        self.scope = scope
        self.detail = detail
        super().__init__(f"AgentBudgetExceeded({scope}): {detail}")


class AgentDecisionError(AgentError):
    """LLM output failed typed AgentDecision validation."""


class AgentRoleError(AgentError):
    """Requested role is not present in the agent config."""


class AgentReplayError(AgentError):
    """Journal replay payload is corrupt or incomplete (fail-closed)."""
