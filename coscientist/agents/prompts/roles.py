"""Versioned system prompts for planner / executor-route / tz_spec (CAP #27)."""

from __future__ import annotations

from coscientist.agents.prompts.registry import PromptRef, PromptRegistry, PromptTemplate

ROLE_PLANNER_REF = PromptRef(prompt_id="role.planner", version="1")
ROLE_EXECUTOR_REF = PromptRef(prompt_id="role.executor_route", version="1")
ROLE_TZ_SPEC_REF = PromptRef(prompt_id="role.tz_spec", version="1")

_ROLE_BODY = "You are the {{role}} role. Reply with JSON matching the response_format schema only."

_PLANNER_BODY = (
    _ROLE_BODY
    + " PLAN CONTRACT (violations are refused):"
    " (1) Task `criteria` accept ONLY ids from the context's"
    " criterion_template_ids (empty list there ⇒ empty criteria); criteria are"
    " for MEASURED success values only — artifact delivery is expressed via"
    " expected_outputs rubrics (existence checks are added automatically)."
    " (2) Task `tools_hint` entries are EXACTLY ids from the context's"
    " tool_ids — copy verbatim, no URLs or descriptions."
    " (3) Prefer question final_type='artifact' (delivered artifact/output);"
    " final_type 'verdict' or 'both' is only for measured conclusions and"
    " requires at least one criterion on an addressing task."
    " (4) A task output delivered by a tool call is named EXACTLY like the"
    " tool id (the tool result artifact is recorded under that id) — do not"
    " invent file names."
    " (5) Rubric item check='code' needs code_kind from exactly:"
    " artifact_exists, schema_valid, json_pointer, source_link,"
    " metric_threshold, value_matches, quote_in_source, exec_runs,"
    " tests_pass, metric_on_holdout, reproducible_rerun; check='rubric'"
    " needs no code_kind."
)


def role_prompt_registry() -> PromptRegistry:
    """Build the in-memory registry for M1 planner / executor-route / tz_spec prompts."""
    templates = (
        PromptTemplate(
            prompt_id=ROLE_PLANNER_REF.prompt_id,
            version=ROLE_PLANNER_REF.version,
            body=_PLANNER_BODY,
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
