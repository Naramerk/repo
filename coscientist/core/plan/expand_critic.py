"""Deterministic M1 PLAN-n critic gate (DESIGN_M1 §4).

LLM critic is M2. M1 runs this core gate on the pending ExpansionRequest
before ``PlanExpanded``. Reject → ``ExpansionRefused(critic_rejected, …)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coscientist.core.events.errors import JournalFormatError
from coscientist.core.execution.domain import (
    ExpansionRequest,
    PlanRole,
    RequirementRef,
    TaskSpec,
    TaskState,
    TemplateKind,
)
from coscientist.core.plan.expand_validate import (
    active_plan_blob,
    assign_core_keys,
    validate_expansion,
)
from coscientist.core.plan.supersede import (
    target_blocks_agent_supersede,
    task_protected_from_agent_skip,
)

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime

# Explicit PLAN-n criteria (must exist in catalog with kind=plan_n_critic).
PLAN_N_CRITIC_CRITERIA: list[RequirementRef] = [
    RequirementRef(template_id="plan_expansion_schema", required=True),
    RequirementRef(template_id="plan_expansion_depth_quota_reserve", required=True),
    RequirementRef(template_id="plan_expansion_no_refutation_escape", required=True),
    RequirementRef(template_id="plan_expansion_acceptance_criteria", required=True),
    RequirementRef(template_id="plan_expansion_no_duplicates", required=True),
]


def task_is_plan_n(task: Any) -> bool:
    """True when ``Task.plan_role == PLAN_N`` (sole wire signal).

    Missing / invalid ``plan_role`` raises (fail-closed) — never inferred from
    criteria / key prefix / silent False. Only ``PlanRole`` or ``str`` accepted
    (no ``str(role)`` coercion of arbitrary objects).
    """
    try:
        role = task.plan_role
    except AttributeError as e:
        raise JournalFormatError("task missing required plan_role") from e
    if role is None:
        raise JournalFormatError("task.plan_role must not be null")
    if isinstance(role, PlanRole):
        return role == PlanRole.PLAN_N
    if isinstance(role, str):
        try:
            return PlanRole(role) == PlanRole.PLAN_N
        except ValueError as e:
            raise JournalFormatError(f"task.plan_role invalid: {role!r}") from e
    raise JournalFormatError(
        f"task.plan_role must be PlanRole|str, got {type(role).__name__}"
    )




def next_plan_n_key(*, expansions: int, existing_plan_n: int) -> str:
    """Single PLAN-n id allocator (catalog markers + expand_trigger share this)."""
    if type(expansions) is not int or type(existing_plan_n) is not int:
        raise TypeError("expansions and existing_plan_n must be exact int")
    if expansions < 0 or existing_plan_n < 0:
        raise ValueError("expansions/existing_plan_n must be >= 0")
    return f"PLAN-{expansions + existing_plan_n + 1}"

def critic_gate(
    runtime: StudyRuntime,
    req: ExpansionRequest,
    *,
    after_task: str,
) -> str | None:
    """Return rejection detail, or None if the request passes the M1 critic gate.

    Checks: schema/DAG/addresses, depth/quota/reserve, no refutation escape,
    new tasks have real catalog acceptance criteria (no plan_expansion_* markers),
    no duplicates of existing tasks.
    """
    study = runtime.study
    if study is None:
        return "no_study"
    catalog = runtime.catalog
    if catalog is None:
        return "missing_catalog"
    templates = list(catalog.templates)
    by_id = {t.id: t for t in templates}
    keyed = assign_core_keys(runtime, list(req.tasks))
    if isinstance(keyed, str):
        return keyed
    # Every criterion must resolve in catalog; plan_n_critic markers forbidden on agent tasks.
    for spec in keyed:
        crits = list(spec.criteria)
        if not crits:
            return f"missing_criteria:{spec.task_key}"
        for ref in crits:
            tmpl_id = ref.template_id
            if not isinstance(tmpl_id, str) or not tmpl_id:
                return f"missing_template_id:{spec.task_key}"
            tmpl = by_id.get(tmpl_id)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
            if tmpl is None:
                return f"unknown_template_id:{tmpl_id}:{spec.task_key}"
            if tmpl.kind == TemplateKind.PLAN_N_CRITIC:
                return f"forbidden_plan_expansion_marker:{spec.task_key}"
    prev = active_plan_blob(runtime)
    if prev is None:
        prev_tasks = []
        known: dict[str, Any] = {}  # audit-ok[field_default]: structural empty = no prior questions (absent)
    else:
        prev_tasks = [
            TaskSpec.model_validate(t) if isinstance(t, dict) else t  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
            for t in prev["tasks"]
        ]
        known = {}
        for q in prev["questions"]:
            if isinstance(q, dict):
                if "id" not in q:
                    return "plan_question_missing_id"
                qid = q["id"]
                if not isinstance(qid, str) or not qid:
                    return "plan_question_id_must_be_nonempty_str"
                if "text" not in q or not isinstance(q["text"], str):
                    return f"missing_question_text:{qid}"
                known[qid] = q["text"]
            else:
                known[q.id] = q.text
    known.update({q.id: q.text for q in req.questions})
    # PR12: agents cannot raise their own budget_share (before Σ validation).
    from coscientist.core.plan.shares import agent_raises_budget_share

    raise_detail = agent_raises_budget_share(
        prev_tasks=prev_tasks,
        added_tasks=keyed,
        after_task=after_task,
    )
    if raise_detail is not None:
        return raise_detail
    # MaxDepthExceeded propagates — callers map it to ExpansionRefused(expansion_limit).
    err = validate_expansion(
        runtime,
        added_tasks=keyed,
        added_questions=list(req.questions),
        superseded_tasks=list(req.superseded_tasks),
        superseded_questions=list(req.superseded_questions),
        known_texts=known,
        prev_tasks=prev_tasks,
        active_q_ids=set(known),
    )
    if err is not None:
        return err
    expansions = runtime._agent_expansions
    max_exp = study.max_expansions
    if type(expansions) is not int or type(max_exp) is not int:
        raise JournalFormatError(
            "agent_expansions/max_expansions must be exact int "
            f"(got {type(expansions).__name__}/{type(max_exp).__name__})"
        )
    if expansions >= max_exp:
        return "agent_quota"
    # No refutation escape (MJ-b).
    for s in req.superseded_questions:
        block = target_blocks_agent_supersede(runtime, s.old_id)
        if block:
            return f"refutation_escape:{s.old_id}:{block}"
    for tkey in req.superseded_tasks:
        rt_task = None
        for t in runtime.tasks.values():
            if t.spec.task_key == tkey:
                rt_task = t
                break
        if rt_task is None:
            continue
        if rt_task.state != TaskState.DONE and task_protected_from_agent_skip(runtime, rt_task):
            return f"refutation_escape:{tkey}:open_refute_task"
        for qid in rt_task.spec.addresses:
            block = target_blocks_agent_supersede(runtime, qid)
            if block:
                return f"refutation_escape:{qid}:{block}"
    existing_keys = {t.spec.task_key for t in runtime.tasks.values()}
    for spec in keyed:
        if spec.task_key in existing_keys:
            return f"duplicate_task:{spec.task_key}"
    for spec in keyed:
        for t in runtime.tasks.values():
            if _same_task_shape(t.spec, spec):
                return f"duplicate_shape:{spec.task_key}"
    _ = after_task
    return None


def _same_task_shape(a: TaskSpec, b: TaskSpec) -> bool:
    return (
        a.goal == b.goal
        and a.route == b.route
        and sorted(a.addresses) == sorted(b.addresses)
        and sorted(a.depends_on) == sorted(b.depends_on)
        and a.title == b.title
    )


def prepare_request_for_apply(
    runtime: StudyRuntime, req: ExpansionRequest
) -> ExpansionRequest | str:
    """Assign core keys; return updated request or error detail."""
    keyed = assign_core_keys(runtime, list(req.tasks))
    if isinstance(keyed, str):
        return keyed
    return ExpansionRequest(
        tasks=keyed,
        questions=list(req.questions),
        superseded_tasks=list(req.superseded_tasks),
        superseded_questions=list(req.superseded_questions),
        reason=req.reason,
    )
