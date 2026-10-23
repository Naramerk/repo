"""PlanExpanded apply path: quotas, replan_reserve, question supersede (M1 PR9).

Trigger lives in ``expand_trigger``; validation in ``expand_validate``;
critic gate in ``expand_critic``. Never raise out of tick/record.
``delegated_by`` is derived only from a matching ``HitlDecided`` (MJ-d).
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
from coscientist.core.expand_critic import critic_gate, prepare_request_for_apply
from coscientist.core.expand_graph import (
    plan_graph_events,
    question_graph_events,
    skip_superseded_task_events,
)
from coscientist.core.expand_trigger import refuse_expansion
from coscientist.core.expand_validate import (
    active_plan_blob,
    assign_core_keys,
    task_produced_countable_claims,
    validate_expansion,
)
from coscientist.core.ids import plan_id, task_id
from coscientist.core.supersede import (
    should_skip_for_question_supersede,
    target_blocks_agent_supersede,
    tasks_needed_by_active_questions,
)

if TYPE_CHECKING:
    from coscientist.core.machine import StudyRuntime

Origin = Literal["plan", "emergent"]
DelegatedBy = Literal["human", "timeout"] | None


def _quota_kind(delegated_by: DelegatedBy) -> Literal["agent", "human"]:
    # HITL path (human accept or decide-yourself / timeout) uses human quota.
    return "human" if delegated_by in ("human", "timeout") else "agent"


def _delegated_by_from_hitl(
    runtime: StudyRuntime, hitl_request_id: str
) -> DelegatedBy | str:
    """Derive delegated_by from HitlDecided only. Return error detail if missing."""
    decisions = getattr(runtime, "_hitl_decisions", None)
    row: dict | None = None
    if isinstance(decisions, dict):
        row = decisions.get(hitl_request_id)
    if row is None:
        hitl = getattr(runtime, "hitl", None) or {}
        h = hitl.get(hitl_request_id) if isinstance(hitl, dict) else None
        if h is not None and not getattr(h, "open", True):
            # Closed HITL without a folded decision is still invalid for forge.
            pass
        return "missing_hitl_decided"
    db = row.get("delegated_by")
    if db in ("human", "timeout"):
        return db  # type: ignore[return-value]
    # Human accept (approved_by=human, no delegated_by) → treat as human quota.
    if row.get("approved_by") == "human" or row.get("decided_by") == "human":
        return "human"
    if row.get("approved_by") == "critic" or row.get("decided_by") == "critic":
        return "timeout" if db == "timeout" else "human"
    return "missing_hitl_decided"


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
    hitl_request_id: str | None = None,
    expand_after: list[str] | None = None,
    assign_keys: bool = False,
    replan_allocated_s: float | None = None,
) -> str | None:
    """Apply a plan expansion. Returns new plan_id, or None if ExpansionRefused.

    ``delegated_by`` is not a public argument (MJ-d): pass ``hitl_request_id``
    and it is taken from the matching ``HitlDecided``; unknown id →
    ``ExpansionRefused(invalid_hitl)``.
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
            hitl_request_id=hitl_request_id,
            expand_after=expand_after,
            assign_keys=assign_keys,
            replan_allocated_s=replan_allocated_s,
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
    hitl_request_id: str | None,
    expand_after: list[str] | None,
    assign_keys: bool,
    replan_allocated_s: float | None,
) -> str | None:
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return None
    study = runtime.study
    sid = study.study_id
    now = runtime.clock.now()

    delegated_by: DelegatedBy = None
    if hitl_request_id:
        derived = _delegated_by_from_hitl(runtime, hitl_request_id)
        if isinstance(derived, str) and derived not in ("human", "timeout"):
            await refuse_expansion(
                runtime, after_task, reason="invalid_hitl", detail=str(derived)[:200]
            )
            return None
        delegated_by = derived  # type: ignore[assignment]

    added_questions = list(added_questions or [])
    superseded_tasks = list(superseded_tasks or [])
    superseded_questions = list(superseded_questions or [])
    expand_after = list(expand_after or [])

    if assign_keys:
        keyed = assign_core_keys(runtime, list(added_tasks))
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
    if replan_allocated_s is not None:
        allocate = float(replan_allocated_s)
    else:
        allocate = min(float(study.plan_timeout_s), remaining_budget, time_left)
        if remaining_budget <= 0 or allocate <= 0:
            await refuse_expansion(
                runtime, after_task, reason="expansion_limit", detail="replan_reserve"
            )
            return None

    # MJ-b: agent path cannot supersede/SKIP past countable refutation or open refute.
    blocked_qs: set[str] = set()
    if qkind == "agent":
        for s in superseded_questions:
            block = target_blocks_agent_supersede(runtime, s.old_id)
            if block:
                await refuse_expansion(
                    runtime, after_task, reason="has_refutation", detail=f"{s.old_id}:{block}"
                )
                return None
            blocked_qs.add(s.old_id)

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
            blocked_questions=blocked_qs if qkind == "agent" else None,
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
        kind = TaskKind.EXPERIMENT
        route = (spec.route or "").lower()
        if route in ("literature", "lit"):
            kind = TaskKind.LITERATURE
        elif route in ("hypotheses", "hypothesis", "hyp"):
            kind = TaskKind.HYPOTHESES
        elif route in ("planner", "plan"):
            kind = TaskKind.PLAN
        task = Task(
            task_id=tid,
            kind=kind,
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
    """Run M1 critic gate, then apply queued ExpansionRequest after PLAN-n SUCCESS."""
    reqs = getattr(runtime, "_expand_requests", None) or {}
    meta = reqs.get(plan_key) if isinstance(reqs, dict) else None
    if not isinstance(meta, dict):
        return None
    after = str(meta.get("after_task") or plan_key)
    raw = meta.get("pending")
    if not isinstance(raw, dict):
        await refuse_expansion(runtime, after, reason="critic_rejected", detail="no_pending")
        if isinstance(reqs, dict):
            reqs.pop(plan_key, None)
        return None
    try:
        req = ExpansionRequest.model_validate(raw)
    except Exception:  # noqa: BLE001
        await refuse_expansion(runtime, after, reason="critic_rejected", detail="pending_bad")
        if isinstance(reqs, dict):
            reqs.pop(plan_key, None)
        return None
    reject = critic_gate(runtime, req, after_task=after)
    if reject is not None:
        await refuse_expansion(runtime, after, reason="critic_rejected", detail=str(reject)[:200])
        if isinstance(reqs, dict):
            reqs.pop(plan_key, None)
        return None
    prepared = prepare_request_for_apply(runtime, req)
    if isinstance(prepared, str):
        await refuse_expansion(runtime, after, reason="critic_rejected", detail=prepared[:200])
        if isinstance(reqs, dict):
            reqs.pop(plan_key, None)
        return None
    # PLAN-n already reserved replan time — do not double-count on PlanExpanded.
    already = float(meta.get("replan_allocated_s") or 0.0)
    pid = await expand_plan(
        runtime,
        after_task=after,
        added_tasks=list(prepared.tasks),
        added_questions=list(prepared.questions),
        superseded_tasks=list(prepared.superseded_tasks),
        superseded_questions=list(prepared.superseded_questions),
        origin="emergent",
        reason=prepared.reason or "expansion_request",
        assign_keys=False,  # already assigned in prepare_request_for_apply
        replan_allocated_s=0.0 if already > 0 else None,
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
