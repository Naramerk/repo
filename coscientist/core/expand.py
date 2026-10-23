"""PlanExpanded apply path: quotas, replan_reserve, question supersede (M1 PR9).

Trigger lives in ``expand_trigger``; validation in ``expand_validate``;
graph helpers in ``expand_graph``. Never raise out of tick/record — invalid
requests become ``ExpansionRefused(reason=invalid)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from coscientist.core.domain import (
    ExpansionRequest,
    Plan,
    QuestionSpec,
    QuestionSupersede,
    Task,
    TaskKind,
    TaskSpec,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.expand_graph import (
    plan_graph_events,
    question_graph_events,
    skip_superseded_task_events,
)
from coscientist.core.expand_trigger import refuse_expansion
from coscientist.core.expand_validate import (
    active_plan_blob,
    assign_exp_keys,
    task_produced_countable_claims,
    validate_expansion,
)
from coscientist.core.ids import plan_id, task_id
from coscientist.core.supersede import (
    question_has_countable_refutation,
    should_skip_for_question_supersede,
    tasks_needed_by_active_questions,
)

if TYPE_CHECKING:
    from coscientist.core.machine import StudyRuntime

Origin = Literal["plan", "emergent"]
DelegatedBy = Literal["human", "timeout"] | None


def _quota_kind(delegated_by: DelegatedBy) -> Literal["agent", "human"]:
    return "human" if delegated_by == "human" else "agent"


def _delegated_by_from_hitl(runtime: StudyRuntime, hitl_request_id: str | None) -> DelegatedBy:
    """MJ6: derive delegated_by from HitlDecided, not from a free argument alone."""
    if not hitl_request_id:
        return None
    hitl = getattr(runtime, "hitl", None) or {}
    h = hitl.get(hitl_request_id) if isinstance(hitl, dict) else None
    if h is not None:
        db = getattr(h, "delegated_by", None)
        if db in ("human", "timeout"):
            return db  # type: ignore[return-value]
        raw = getattr(h, "decision_payload", None) or {}
        if isinstance(raw, dict) and raw.get("delegated_by") in ("human", "timeout"):
            return raw["delegated_by"]  # type: ignore[return-value]
    decisions = getattr(runtime, "_hitl_decisions", None)
    if isinstance(decisions, dict) and hitl_request_id in decisions:
        db = decisions[hitl_request_id].get("delegated_by")
        if db in ("human", "timeout"):
            return db  # type: ignore[return-value]
    return "human"


async def expand_plan(
    runtime: StudyRuntime,
    *,
    after_task: str,
    added_tasks: list[TaskSpec],
    added_questions: list[QuestionSpec] | None = None,
    superseded_tasks: list[str] | None = None,
    superseded_questions: list[QuestionSupersede] | None = None,
    origin: Origin = "emergent",
    reason: str = "",
    delegated_by: DelegatedBy = None,
    hitl_request_id: str | None = None,
    expand_after: list[str] | None = None,
    assign_keys: bool = False,
) -> str | None:
    """Apply a plan expansion. Returns new plan_id, or None if ExpansionRefused.

    Agent-path pending apply sets ``assign_keys=True`` (core EXP-<n>); HITL/API
    may pass explicit keys with ``assign_keys=False``.
    """
    try:
        return await _expand_plan(
            runtime,
            after_task=after_task,
            added_tasks=added_tasks,
            added_questions=added_questions,
            superseded_tasks=superseded_tasks,
            superseded_questions=superseded_questions,
            origin=origin,
            reason=reason,
            delegated_by=delegated_by,
            hitl_request_id=hitl_request_id,
            expand_after=expand_after,
            assign_keys=assign_keys,
        )
    except Exception:  # noqa: BLE001 — never raise to tick/record
        await refuse_expansion(runtime, after_task, reason="invalid", detail="expand_crash")
        return None


async def _expand_plan(
    runtime: StudyRuntime,
    *,
    after_task: str,
    added_tasks: list[TaskSpec],
    added_questions: list[QuestionSpec] | None,
    superseded_tasks: list[str] | None,
    superseded_questions: list[QuestionSupersede] | None,
    origin: Origin,
    reason: str,
    delegated_by: DelegatedBy,
    hitl_request_id: str | None,
    expand_after: list[str] | None,
    assign_keys: bool,
) -> str | None:
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return None
    study = runtime.study
    sid = study.study_id
    now = runtime.clock.now()
    if hitl_request_id:
        delegated_by = _delegated_by_from_hitl(runtime, hitl_request_id)
    added_questions = list(added_questions or [])
    superseded_tasks = list(superseded_tasks or [])
    superseded_questions = list(superseded_questions or [])
    expand_after = list(expand_after or [])

    if assign_keys:
        keyed = assign_exp_keys(runtime, list(added_tasks))
        if isinstance(keyed, str):
            await refuse_expansion(runtime, after_task, reason="invalid", detail=keyed[:200])
            return None
        added_tasks = keyed
    else:
        added_tasks = list(added_tasks)

    qkind = _quota_kind(delegated_by)
    agent_n = int(getattr(runtime, "_agent_expansions", 0))
    human_n = int(getattr(runtime, "_human_expansions", 0))
    if qkind == "human":
        if human_n >= study.max_human_expansions:
            await refuse_expansion(runtime, after_task, reason="expansion_limit", detail="human_quota")
            return None
    elif agent_n >= study.max_expansions:
        await refuse_expansion(runtime, after_task, reason="expansion_limit", detail="agent_quota")
        return None

    used = float(getattr(runtime, "_replan_used_s", 0.0))
    budget = study.replan_budget_s()
    remaining_budget = budget - used
    ed = study.effective_deadline()
    time_left = max(0.0, (ed - now).total_seconds())
    allocate = min(float(study.plan_timeout_s), remaining_budget, time_left)
    if remaining_budget <= 0 or allocate <= 0:
        await refuse_expansion(runtime, after_task, reason="expansion_limit", detail="replan_reserve")
        return None

    if qkind == "agent":
        for s in superseded_questions:
            if question_has_countable_refutation(runtime, s.old_id):
                await refuse_expansion(
                    runtime, after_task, reason="has_refutation", detail=s.old_id
                )
                return None

    prev = active_plan_blob(runtime)
    prev_rev = int(prev.get("revision") or 0) if prev else 0
    prev_pid = str(prev.get("plan_id") or "") if prev else ""
    new_rev = prev_rev + 1
    prev_tasks = [
        TaskSpec.model_validate(t) if isinstance(t, dict) else t
        for t in (prev.get("tasks") if prev else []) or []
    ]
    known_texts: dict[str, str] = {}
    for q in (prev.get("questions") if prev else []) or []:
        if isinstance(q, dict) and q.get("id"):
            known_texts[str(q["id"])] = str(q.get("text") or "")
        elif hasattr(q, "id"):
            known_texts[str(q.id)] = str(q.text)
    known_texts.update({q.id: q.text for q in added_questions})

    superseded_old = {s.old_id for s in superseded_questions}
    active_q_after: set[str] = {qid for qid in known_texts if qid not in superseded_old}
    for q in added_questions:
        active_q_after.add(q.id)

    err = validate_expansion(
        runtime,
        added_tasks=added_tasks,
        added_questions=added_questions,
        superseded_tasks=superseded_tasks,
        superseded_questions=superseded_questions,
        known_texts=known_texts,
        prev_tasks=prev_tasks,
        active_q_ids=active_q_after,
    )
    if err is not None:
        reason_code = (
            "expansion_limit" if err in ("max_tasks",) or str(err).startswith("max_depth") else "invalid"
        )
        await refuse_expansion(runtime, after_task, reason=reason_code, detail=str(err)[:200])
        return None

    skip_keys = {k for k in superseded_tasks if not task_produced_countable_claims(runtime, k)}
    kept = [t for t in prev_tasks if t.task_key not in skip_keys]
    protected = tasks_needed_by_active_questions(
        tasks=kept + list(added_tasks),
        superseded_old=superseded_old,
        active_q_after=active_q_after,
    )
    for t in kept:
        if task_produced_countable_claims(runtime, t.task_key):
            protected.add(t.task_key)

    still_kept: list[TaskSpec] = []
    for t in kept:
        tid_exist = task_id(sid, t.task_key)
        rt_task = runtime.tasks.get(tid_exist)
        already_done = rt_task is not None and rt_task.state == TaskState.DONE
        if already_done:
            still_kept.append(t)
            continue
        if should_skip_for_question_supersede(
            task=t,
            superseded_old=superseded_old,
            active_q_after=active_q_after,
            protected_keys=protected,
        ):
            skip_keys.add(t.task_key)
        else:
            still_kept.append(t)
    superseded_tasks = sorted(skip_keys)
    merged_tasks = still_kept + list(added_tasks)

    prev_questions = [
        QuestionSpec.model_validate(q) if isinstance(q, dict) else q
        for q in (prev.get("questions") if prev else []) or []
    ]
    all_questions = [q for q in prev_questions if q.id not in superseded_old] + added_questions

    pid = plan_id()
    plan = Plan(
        plan_id=pid,
        study_id=sid,
        revision=new_rev,
        hypothesis_id=None,
        tasks=merged_tasks,
        questions=all_questions,
        expand_after=expand_after,
    )
    events: list[EventEnvelope] = []
    events.extend(
        plan_graph_events(
            runtime,
            sid=sid,
            pid=pid,
            prev_pid=prev_pid,
            new_rev=new_rev,
            prev_rev=prev_rev,
            origin=origin,
            delegated_by=delegated_by,
            now=now,
        )
    )
    events.extend(
        question_graph_events(
            runtime,
            sid=sid,
            pid=pid,
            added_questions=added_questions,
            superseded_questions=superseded_questions,
            known_texts=known_texts,
            now=now,
        )
    )
    events.extend(skip_superseded_task_events(runtime, sid=sid, superseded_tasks=superseded_tasks, now=now))
    existing_keys = {t.spec.task_key for t in runtime.tasks.values()}
    for spec in added_tasks:
        if spec.task_key in existing_keys and spec.task_key not in skip_keys:
            continue
        tid = task_id(sid, spec.task_key)
        task = Task(
            task_id=tid,
            kind=TaskKind.EXPERIMENT,
            spec=spec,
            state=TaskState.PENDING,
            deadline=ed,
        )
        events.append(
            EventEnvelope(
                study_id=sid,
                type="TaskCreated",
                entity_key=tid,
                idem_key=make_idem("TaskCreated", tid, pid),
                payload=task.model_dump(mode="json"),
                ts=now,
                schema_version=6,
            )
        )

    payload = {
        **plan.model_dump(mode="json"),
        "after_task": after_task,
        "plan_rev": new_rev,
        "added_tasks": [t.model_dump(mode="json") for t in added_tasks],
        "added_questions": [q.model_dump(mode="json") for q in added_questions],
        "superseded_tasks": superseded_tasks,
        "superseded_questions": [s.model_dump(mode="json") for s in superseded_questions],
        "origin": origin,
        "reason": reason,
        "delegated_by": delegated_by,
        "replan_allocated_s": allocate,
        "prev_plan_id": prev_pid or None,
        "hitl_request_id": hitl_request_id,
    }
    events.append(
        EventEnvelope(
            study_id=sid,
            type="PlanExpanded",
            entity_key=pid,
            idem_key=make_idem("PlanExpanded", pid),
            payload=payload,
            ts=now,
            schema_version=1,
        )
    )
    await runtime._append(events)  # noqa: SLF001
    return pid


async def apply_pending_expansion(runtime: StudyRuntime, plan_key: str) -> str | None:
    """Apply queued ExpansionRequest after PLAN-n SUCCESS (critic accept)."""
    reqs = getattr(runtime, "_expand_requests", None) or {}
    meta = reqs.get(plan_key) if isinstance(reqs, dict) else None
    if not isinstance(meta, dict):
        return None
    after = str(meta.get("after_task") or plan_key)
    raw = meta.get("pending")
    if not isinstance(raw, dict):
        return None
    try:
        req = ExpansionRequest.model_validate(raw)
    except Exception:  # noqa: BLE001
        await refuse_expansion(runtime, after, reason="invalid", detail="pending_bad")
        return None
    pid = await expand_plan(
        runtime,
        after_task=after,
        added_tasks=list(req.tasks),
        added_questions=list(req.questions),
        superseded_tasks=list(req.superseded_tasks),
        superseded_questions=list(req.superseded_questions),
        origin="emergent",
        reason=req.reason or "expansion_request",
        delegated_by=None,
        assign_keys=True,
    )
    if isinstance(reqs, dict):
        reqs.pop(plan_key, None)
    return pid


from coscientist.core.expand_trigger import maybe_expand_after_task_done  # noqa: E402
from coscientist.core.supersede import (  # noqa: E402
    criterion_is_superseded,
    question_is_superseded,
)

__all__ = [
    "apply_pending_expansion",
    "expand_plan",
    "maybe_expand_after_task_done",
    "question_is_superseded",
    "criterion_is_superseded",
]
