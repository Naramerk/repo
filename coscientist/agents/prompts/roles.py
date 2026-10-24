"""Versioned system prompts for planner / executor-route (CAP #27)."""

from __future__ import annotations

from coscientist.agents.prompts.registry import PromptRef, PromptRegistry, PromptTemplate

ROLE_PLANNER_REF = PromptRef(prompt_id="role.planner", version="1")
ROLE_EXECUTOR_REF = PromptRef(prompt_id="role.executor_route", version="1")

_PLANNER_BODY = "You are the {{role}} role. Reply with JSON matching the response_format schema only."
_EXECUTOR_BODY = "You are the {{role}} role. Reply with JSON matching the response_format schema only."


def role_prompt_registry() -> PromptRegistry:
    """Build the in-memory registry for M1 planner / executor-route prompts."""
    planner = PromptTemplate(
        prompt_id=ROLE_PLANNER_REF.prompt_id,
        version=ROLE_PLANNER_REF.version,
        body=_PLANNER_BODY,
        required_variables=("role",),
    )
    executor = PromptTemplate(
        prompt_id=ROLE_EXECUTOR_REF.prompt_id,
        version=ROLE_EXECUTOR_REF.version,
        body=_EXECUTOR_BODY,
        required_variables=("role",),
    )
    return PromptRegistry(
        {
            (planner.prompt_id, planner.version): planner,
            (executor.prompt_id, executor.version): executor,
        }
    )
