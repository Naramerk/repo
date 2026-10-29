"""PlanExpanded apply path: quotas, replan_reserve, question supersede (M1 PR9).

Trigger lives in ``expand_trigger``; validation in ``expand_validate``;
critic gate in ``expand_critic``. Never raise out of tick/record.
``delegated_by`` is derived only from a matching ``HitlDecided`` (MJ-d).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from coscientist.core.events.errors import CatalogError, LeaseLost
from coscientist.core.events.events import (
    PLAN_EXPANDED_SCHEMA_VERSION,
    TASK_CREATED_SCHEMA_VERSION,
    EventEnvelope,
    make_idem,
)
from coscientist.core.events.ids import plan_id, task_id
from coscientist.core.execution.domain import (
    ExpansionRequest,
    Plan,
    QuestionSpec,
    QuestionSupersede,
    Task,
    TaskSpec,
    TaskState,
)
from coscientist.core.finalize.finalize import FINALIZE_SOFT_FAILURES
from coscientist.core.plan.expand_critic import critic_gate, prepare_request_for_apply
from coscientist.core.plan.expand_graph import (
    plan_graph_events,
    question_graph_events,
    skip_superseded_task_events,
)
from coscientist.core.plan.expand_trigger import refuse_expansion
from coscientist.core.plan.expand_validate import (
    active_plan_blob,
    assign_core_keys,
    task_kind_for_route,
    task_produced_countable_claims,
    validate_expansion,
)
from coscientist.core.plan.need import PlanNeedRefuse, recorded_plan_needs
from coscientist.core.plan.supersede import (
    should_skip_for_question_supersede,
    target_blocks_agent_supersede,
    task_protected_from_agent_skip,
    tasks_needed_by_active_questions,
)

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime

Origin = Literal["plan", "emergent"]
DelegatedBy = Literal["human", "timeout"] | None


def _quota_kind(delegated_by: DelegatedBy) -> Literal["agent", "human"]:
    # DESIGN_M1 §4: only delegated_by=human uses human quota. timeout → agent
    # (and goes through MJ-b refutation checks).
    return "human" if delegated_by == "human" else "agent"


_PLAN_EXPAND_HITL_KINDS = frozenset({"plan", "plan_approval", "plan_expansion"})


def _delegated_by_from_hitl(
    runtime: StudyRuntime,
    hitl_request_id: str,
    *,
    after_task: str,
    plan_key: str | None = None,
) -> DelegatedBy | str:
    """Derive delegated_by from HitlDecided only. Return error detail if missing/reused.

    Plan change → expansion when ``HitlOpened.kind`` is plan / plan_approval /
    plan_expansion **and** ``expand_after_task`` is set and matches this
    ``after_task`` (or PLAN-n key). Kind is not renamed; GateConfig stays on
    the original kind.
    """
    used = getattr(runtime, "_hitl_expand_used", None)
    if isinstance(used, set) and hitl_request_id in used:
        return "hitl_already_used"
    decisions = getattr(runtime, "_hitl_decisions", None)
    row: dict | None = None
    if isinstance(decisions, dict):
        row = decisions.get(hitl_request_id)
    if row is None:
        return "missing_hitl_decided"
    opened = getattr(runtime, "_hitl_requests", None) or {}
    req = opened.get(hitl_request_id) if isinstance(opened, dict) else None
    if req is None:
        hitl = getattr(runtime, "hitl", None) or {}
        h = hitl.get(hitl_request_id) if isinstance(hitl, dict) else None
        if h is not None:
            req = {
                "kind": getattr(h, "kind", None),
                "task_id": getattr(h, "task_id", None),
                "expand_after_task": getattr(h, "expand_after_task", None)
                if hasattr(h, "expand_after_task")
                else (getattr(h, "__dict__", {}) or {}).get("expand_after_task"),
            }
    if req is None:
        return "missing_hitl_opened"
    kind = str(req.get("kind") or "")
    if kind not in _PLAN_EXPAND_HITL_KINDS:
        return f"hitl_wrong_kind:{kind or 'unknown'}"
    # Expansion only when expand_after_task is explicitly set (no task_key fallback).
    bound = str(req.get("expand_after_task") or "")
    if not bound:
        return "hitl_unbound"
    if bound not in {after_task, plan_key or ""}:
        return f"hitl_wrong_target:{bound}"
    db = row.get("delegated_by")
    if db in ("human", "timeout"):
        return db  # type: ignore[return-value]
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
    plan_key: str | None = None,
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
            plan_key=plan_key,
        )
    except LeaseLost:
        raise
    except CatalogError:
        # Catalog / freeze refuse must not be swallowed as expand_crash.
        raise
    except FINALIZE_SOFT_FAILURES:
        # Soft failure while applying an *invalid* expansion → ExpansionRefused
        # (expand_crash) so tick/record never crash. CatalogError / LeaseLost escape.
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
    plan_key: str | None,
) -> str | None:
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return None
    study = runtime.study
    sid = study.study_id
    now = runtime.clock.now()

    delegated_by: DelegatedBy = None
    if hitl_request_id:
        derived = _delegated_by_from_hitl(
            runtime, hitl_request_id, after_task=after_task, plan_key=plan_key
        )
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

    # MJ-b: agent path (incl. HITL timeout) cannot supersede/SKIP past refutation.
    blocked_qs: set[str] = set()
    refute_keys: set[str] = set()
    if qkind == "agent":
        for s in superseded_questions:
            block = target_blocks_agent_supersede(runtime, s.old_id)
            if block:
                await refuse_expansion(
                    runtime, after_task, reason="has_refutation", detail=f"{s.old_id}:{block}"
                )
                return None
            blocked_qs.add(s.old_id)
        for rt in runtime.tasks.values():
            if rt.state != TaskState.DONE and task_protected_from_agent_skip(runtime, rt):
                refute_keys.add(rt.spec.task_key)
                if rt.spec.task_key in superseded_tasks:
                    await refuse_expansion(
                        runtime,
                        after_task,
                        reason="has_refutation",
                        detail=f"{rt.spec.task_key}:open_refute_task",
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
            blocked_questions=blocked_qs if qkind == "agent" else None,
            refute_keys=refute_keys if qkind == "agent" else None,
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
    # Stamp expansion origin onto new child questions when unset (PR16).
    from coscientist.core.plan.decomposes import stamp_question_origins

    added_ids = {aq.id for aq in added_questions}
    named_added = {aq.id for aq in added_questions if getattr(aq, "parent_id", None)}
    stamped_new = stamp_question_origins(
        [q for q in all_questions if q.id in added_ids],
        named_sub_ids=named_added,
        plan_origin="emergent" if origin == "emergent" else None,
    )
    by_new = {q.id: q for q in stamped_new}
    all_questions = [by_new.get(q.id, q) for q in all_questions]
    prev_rollup = (prev or {}).get("rollup") if prev else None
    rollup = prev_rollup if prev_rollup in ("all", "any", "majority", "rubric") else "all"
    named_subs = [q for q in all_questions if getattr(q, "parent_id", None)]

    pid = plan_id()
    from coscientist.core.execution.domain import LoopSpec

    prev_loops = [
        LoopSpec.model_validate(x) if isinstance(x, dict) else x
        for x in ((prev.get("loops") if prev else None) or [])
    ]
    need_errors: list[PlanNeedRefuse] = []
    try:
        hyp_need, lit_need = recorded_plan_needs(prev)
    except PlanNeedRefuse as e:
        need_errors.append(e)
    if need_errors:
        refuse = need_errors[0]
        await refuse_expansion(
            runtime, after_task, reason=refuse.reason, detail=str(refuse)[:200]
        )
        return None
    plan = Plan(
        plan_id=pid,
        study_id=sid,
        revision=new_rev,
        hypothesis_id=None,
        tasks=merged_tasks,
        questions=all_questions,
        expand_after=expand_after,
        loops=prev_loops,
        subquestions=named_subs,
        rollup=rollup,  # type: ignore[arg-type]
        hypotheses=hyp_need,
        literature=lit_need,
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
            hypotheses=hyp_need,
            literature=lit_need,
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
    from coscientist.checks.sandbox_limits import freeze_task_sandbox_limits

    catalog = runtime.catalog
    existing_keys = {t.spec.task_key for t in runtime.tasks.values()}
    for spec in added_tasks:
        if spec.task_key in existing_keys and spec.task_key not in skip_keys:
            continue
        frozen_spec = freeze_task_sandbox_limits(spec, catalog)
        tid = task_id(sid, frozen_spec.task_key)
        task = Task(
            task_id=tid,
            kind=task_kind_for_route(frozen_spec.route),
            spec=frozen_spec,
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
                schema_version=TASK_CREATED_SCHEMA_VERSION,
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
        "plan_key": plan_key,
    }
    events.append(
        EventEnvelope(
            study_id=sid,
            type="PlanExpanded",
            entity_key=pid,
            idem_key=make_idem("PlanExpanded", pid),
            payload=payload,
            ts=now,
            schema_version=PLAN_EXPANDED_SCHEMA_VERSION,
        )
    )
    await runtime.append_events(events)
    # B1: newly added foreach templates whose of_task is already DONE.
    from coscientist.core.plan.foreach import materialize_ready_foreach

    await materialize_ready_foreach(runtime)
    return pid


def _resolve_plan_n_request(
    runtime: StudyRuntime, plan_key: str, meta: dict
) -> tuple[ExpansionRequest | None, str | None]:
    """Single source of truth: PLAN-n final-attempt ``expansion_request``.

    Uses ``_final_attempt_id`` only (no seed fallback). Attempt/result
    mismatch → ``invalid`` / ``ExpansionRefused``.
    """
    from coscientist.core.events.ids import task_id as make_task_id
    from coscientist.core.plan.expand_trigger import _final_attempt_id, parse_plan_n_expansion

    _ = meta  # seed pending is advisory for the planner only — never applied here
    sid = runtime.study.study_id if runtime.study is not None else ""
    plan_tid = make_task_id(sid, plan_key) if sid else ""
    if not plan_tid or plan_tid not in runtime.tasks:
        return None, "missing_plan_n_task"
    aid = _final_attempt_id(runtime, plan_tid)
    if not aid:
        return None, "missing_planner_attempt"
    primary = runtime.results.get(aid)
    alternate = runtime.results.get(f"result:{aid}")
    if primary is not None and alternate is not None and dict(primary) != dict(alternate):
        return None, "attempt_result_mismatch"
    row = primary if isinstance(primary, dict) else alternate
    if not isinstance(row, dict) or "expansion_request" not in row:
        return None, "missing_planner_output"
    req, err = parse_plan_n_expansion(runtime, plan_tid)
    if req is None:
        return None, err or "malformed_planner_output"
    return req, None


async def apply_pending_expansion(runtime: StudyRuntime, plan_key: str) -> str | None:
    """Run M1 critic gate, then apply ExpansionRequest after PLAN-n SUCCESS.

    Planner final-attempt ``expansion_request`` is the sole input (DESIGN_M1 §4).
    """
    reqs = getattr(runtime, "_expand_requests", None) or {}
    meta = reqs.get(plan_key) if isinstance(reqs, dict) else None
    if not isinstance(meta, dict):
        return None
    after = str(meta.get("after_task") or plan_key)
    req, err = _resolve_plan_n_request(runtime, plan_key, meta)
    if req is None:
        await refuse_expansion(
            runtime,
            after,
            reason="invalid",
            detail=str(err or "missing_planner_output")[:200],
            plan_key=plan_key,
        )
        return None
    reject = critic_gate(runtime, req, after_task=after)
    if reject is not None:
        await refuse_expansion(
            runtime,
            after,
            reason="critic_rejected",
            detail=str(reject)[:200],
            plan_key=plan_key,
        )
        return None
    prepared = prepare_request_for_apply(runtime, req)
    if isinstance(prepared, str):
        await refuse_expansion(
            runtime, after, reason="critic_rejected", detail=prepared[:200], plan_key=plan_key
        )
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
        plan_key=plan_key,
    )
    # Live clear; fold also clears on PlanExpanded / ExpansionRefused replay.
    if isinstance(reqs, dict):
        reqs.pop(plan_key, None)
    return pid


from coscientist.core.plan.expand_trigger import maybe_expand_after_task_done  # noqa: E402
from coscientist.core.plan.supersede import (  # noqa: E402
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
