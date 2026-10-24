"""Versioned system prompts for planner / executor-route / tz_spec (CAP #27)."""

from __future__ import annotations

from coscientist.agents.prompts.registry import PromptRef, PromptRegistry, PromptTemplate

ROLE_PLANNER_REF = PromptRef(prompt_id="role.planner", version="1")
ROLE_EXECUTOR_REF = PromptRef(prompt_id="role.executor_route", version="1")
ROLE_TZ_SPEC_REF = PromptRef(prompt_id="role.tz_spec", version="1")

_ROLE_BODY = "You are the {{role}} role. Reply with JSON matching the response_format schema only."


def role_prompt_registry() -> PromptRegistry:
    """Build the in-memory registry for M1 planner / executor-route / tz_spec prompts."""
    templates = (
        PromptTemplate(
            prompt_id=ROLE_PLANNER_REF.prompt_id,
            version=ROLE_PLANNER_REF.version,
            body=_ROLE_BODY,
            required_variables=("role",),
        ),
        PromptTemplate(
            prompt_id=ROLE_EXECUTOR_REF.prompt_id,
            version=ROLE_EXECUTOR_REF.version,
            body=_ROLE_BODY,
            required_variables=("role",),
        ),
        PromptTemplate(
            prompt_id=ROLE_TZ_SPEC_REF.prompt_id,
            version=ROLE_TZ_SPEC_REF.version,
            body=_ROLE_BODY,
            required_variables=("role",),
        ),
    )
    return PromptRegistry({(t.prompt_id, t.version): t for t in templates})
