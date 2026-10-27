"""Versioned system prompts for planner / executor-route / tz_spec (CAP #27)."""

from __future__ import annotations

from coscientist.agents.prompts.registry import PromptRef, PromptRegistry, PromptTemplate

ROLE_PLANNER_REF = PromptRef(prompt_id="role.planner", version="2")
ROLE_EXECUTOR_REF = PromptRef(prompt_id="role.executor_route", version="1")
ROLE_TZ_SPEC_REF = PromptRef(prompt_id="role.tz_spec", version="1")
# v2 (this module): the wired CLI plan-critic / post-close reviewer lane. Plain
# role body — the response_format schema echo in the adapter carries the output
# contract and the user payload carries the plan/digest JSON. v1 templates for
# these prompt ids live in ``prompts.builtin`` (designed §7.1 HITL verdict
# contracts, not wired) — the registry versions keep both apart.
ROLE_CRITIC_REF = PromptRef(prompt_id="role.critic", version="2")
ROLE_REVIEWER_REF = PromptRef(prompt_id="role.reviewer", version="2")
# Wired optional roles (CLI --wired-roles; HYP / LIT need stages, DESIGN §7.1).
ROLE_HYPOTHESIZER_REF = PromptRef(prompt_id="role.hypothesizer", version="1")
ROLE_RESEARCHER_REF = PromptRef(prompt_id="role.researcher", version="1")
# Remote coder slice-2: one bounded generation call where the model authors the
# code-task content (text + expected-output workspace paths) for the legacy
# OpenHands sandbox (docs/remote-coder-runtime.md §2). Borrows the required
# executor-route role model — no new profile keys.
ROLE_REMOTE_CODER_REF = PromptRef(prompt_id="role.remote_coder", version="1")

_ROLE_BODY = "You are the {{role}} role. Reply with JSON matching the response_format schema only."

_REMOTE_CODER_BODY = (
    "You are the {{role}} role. Author ONE self-contained code task for a "
    "remote sandbox that executes it in a fresh container. NO input files are "
    "available inside the container — no datasets, no user files — so the task "
    "must be fully self-contained: everything it needs must be produced by the "
    "code itself. Write `task` as a complete instruction: what script or "
    "program to write and run, what it must compute, and what files it must "
    "write. For every expected output, give `name` exactly as listed and "
    "`path` — the workspace-relative file the sandbox must produce it at. "
    "Reply with JSON matching the response_format schema only."
)

# v2: planner emits typed intent (PlanIntent); all frozen-plan internals
# (output naming, rubrics, criteria, coverage) are derived deterministically
# (coscientist.agents.derive) and the tools_hint enum is schema-checked against
# the context's tool_ids (plan_intent_model_for) — the hand-written PLAN
# CONTRACT block is removed (it duplicated the accept_plan gates).


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
        PromptTemplate(
            prompt_id=ROLE_CRITIC_REF.prompt_id,
            version=ROLE_CRITIC_REF.version,
            body=_ROLE_BODY,
            required_variables=("role",),
        ),
        PromptTemplate(
            prompt_id=ROLE_REVIEWER_REF.prompt_id,
            version=ROLE_REVIEWER_REF.version,
            body=_ROLE_BODY,
            required_variables=("role",),
        ),
        PromptTemplate(
            prompt_id=ROLE_HYPOTHESIZER_REF.prompt_id,
            version=ROLE_HYPOTHESIZER_REF.version,
            body=_ROLE_BODY,
            required_variables=("role",),
        ),
        PromptTemplate(
            prompt_id=ROLE_RESEARCHER_REF.prompt_id,
            version=ROLE_RESEARCHER_REF.version,
            body=_ROLE_BODY,
            required_variables=("role",),
        ),
        PromptTemplate(
            prompt_id=ROLE_REMOTE_CODER_REF.prompt_id,
            version=ROLE_REMOTE_CODER_REF.version,
            body=_REMOTE_CODER_BODY,
            required_variables=("role",),
        ),
    )
    return PromptRegistry({(t.prompt_id, t.version): t for t in templates})
