"""H-g expand trigger after TaskDone + ExpansionRefused (M1 PR9).

Agent ``expansion_request`` / ``expand_after`` enqueue a PLAN-n container with
explicit critic criteria. Direct ``PlanExpanded`` is applied after the M1
deterministic critic gate (or HITL) via ``expand_plan`` / ``apply_pending_expansion``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from coscientist.checks.sandbox_limits import freeze_task_sandbox_limits
from coscientist.core.events.errors import LeaseLost
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.ids import task_id
from coscientist.core.events.schema_versions import (
    EXPANSION_REFUSED_SCHEMA_VERSION,
    TASK_CREATED_SCHEMA_VERSION,
)
from coscientist.core.execution.domain import (
    ExpansionRequest,
    PlanRole,
    Route,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.plan.expand_critic import (
    PLAN_N_CRITIC_CRITERIA,
    task_is_plan_n,
)
from coscientist.core.plan.expand_validate import (
    MaxDepthExceeded,
    active_plan_blob,
    assign_core_keys,
    validate_expansion,
)
from coscientist.core.plan.loop import DETAIL_MAX_LEN

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime


def _outcome_token(outcome: TaskOutcome | str | None) -> str:
    if isinstance(outcome, TaskOutcome):
        return outcome.value
    if isinstance(outcome, str) and outcome:
        return outcome
    raise ValueError(f"missing_task_outcome:{outcome!r}")


def expand_after_keys(runtime: StudyRuntime) -> set[str]:
    keys: set[str] = set()
    for plan in runtime.plans.values():
        if "expand_after" not in plan:
            continue
        for k in plan["expand_after"]:
            if k:
                keys.add(str(k))
    return keys

def _final_attempt_id(runtime: StudyRuntime, tid: str) -> str | None:
    task = runtime.tasks.get(tid)
    if task is None or not task.attempts:
        return None
    return task.attempts[-1]

def _result_row_for_attempt(runtime: StudyRuntime, aid: str) -> dict[str, Any]:
    """Sole result key is ``attempt_id`` — no ``result:`` alias dual-lookup."""
    row = runtime.results.get(aid)
    if row is None:
        return {}
    if not isinstance(row, dict):
        return {}
    return dict(row)

def result_expansion_request(runtime: StudyRuntime, tid: str) -> ExpansionRequest | None:
    """Read ExpansionRequest from the FINAL attempt only (MJ3)."""
    req, _err = parse_plan_n_expansion(runtime, tid)
    return req

def parse_plan_n_expansion(
    runtime: StudyRuntime, tid: str
) -> tuple[ExpansionRequest | None, str | None]:
    """Parse PLAN-n / task final-attempt expansion_request.

    Returns ``(req, None)`` on success, ``(None, detail)`` when missing/malformed
    (caller maps detail to ``ExpansionRefused(invalid, …)``).
    """
    aid = _final_attempt_id(runtime, tid)
    if not aid:
        return None, "missing_planner_output"
    row = _result_row_for_attempt(runtime, aid)
    if "expansion_request" not in row:
        return None, "missing_planner_output"
    raw = row.get("expansion_request")
    if raw is None:
        return None, "missing_planner_output"
    if not isinstance(raw, dict):
        return None, "malformed_planner_output"
    try:
        req = ExpansionRequest.model_validate(raw)
    except Exception as e:  # noqa: BLE001 — schema gate
        return None, f"malformed_planner_output:{type(e).__name__}"
    if not req.tasks:
        return None, "empty_planner_expansion"
    return req, None

def _final_result_class(runtime: StudyRuntime, tid: str) -> str | None:
    aid = _final_attempt_id(runtime, tid)
    if not aid:
        return None
    row = _result_row_for_attempt(runtime, aid)
    rc = row.get("result_class")
    return str(rc) if rc else None

def expansion_already_handled(runtime: StudyRuntime, after_task: str) -> bool:
    """True when PlanExpanded / ExpansionRefused / pending PLAN-n exists for after_task."""
    for p in runtime.plans.values():
        if p.get("after_task") == after_task:
            return True
    for r in runtime._expansion_refusals:
        if r.get("after_task") == after_task:
            return True
    for meta in runtime._expand_requests.values():
        if isinstance(meta, dict) and meta.get("after_task") == after_task:
            return True
    return False

async def refuse_expansion(
    runtime: StudyRuntime,
    after_task: str,
    *,
    reason: str,
    detail: str,
    plan_key: str | None = None,
) -> None:
    if runtime.study is None or runtime._finalized:
        return
    sid = runtime.study.study_id
    now = runtime.clock.now()
    # Deterministic idem per PLAN-n (same key on live path and recover).
    if plan_key:
        idem_key = make_idem("ExpansionRefused", sid, plan_key, reason)
        entity_key = f"{sid}:{plan_key}:{reason}"
    else:
        idem_key = make_idem("ExpansionRefused", sid, after_task, reason, detail)
        entity_key = f"{sid}:{after_task}:{reason}:{detail}"
    await runtime._append(  # noqa: SLF001
        [
            EventEnvelope(
                study_id=sid,
                type="ExpansionRefused",
                entity_key=entity_key,
                idem_key=idem_key,
                payload={
                    "after_task": after_task,
                    "reason": reason,
                    "detail": detail,
                    "plan_key": plan_key,
                },
                ts=now,
                schema_version=EXPANSION_REFUSED_SCHEMA_VERSION,
)
        ]
    )

def _agent_quota_ok(runtime: StudyRuntime) -> bool:
    study = runtime.study
    if study is None:
        return False
    return int(runtime._agent_expansions) < int(study.max_expansions)

def _replan_allocate_s(runtime: StudyRuntime) -> float:
    study = runtime.study
    if study is None:
        return 0.0
    now = runtime.clock.now()
    used = float(runtime._replan_used_s)
    budget = study.replan_budget_s() - used
    ed = study.effective_deadline()
    time_left = (ed - now).total_seconds()
    # Refuse negative budgets/time rather than clamping to 0 (fail-closed).
    if budget < 0.0 or time_left < 0.0:
        return 0.0
    return min(float(study.plan_timeout_s), budget, time_left)

async def ensure_plan_n_task(
    runtime: StudyRuntime,
    *,
    after_task: str,
    pending: ExpansionRequest | None = None,
) -> str | None:
    """Create PLAN-<n> with explicit critic criteria (DESIGN_M1 §4 / MJ-a)."""
    if runtime.study is None:
        return None
    if not _agent_quota_ok(runtime):
        await refuse_expansion(runtime, after_task, reason="expansion_limit", detail="agent_quota")
        return None
    allocate = _replan_allocate_s(runtime)
    if allocate <= 0:
        await refuse_expansion(runtime, after_task, reason="expansion_limit", detail="replan_reserve")
        return None
    sid = runtime.study.study_id
    existing_plan_n = sum(
        1 for t in runtime.tasks.values() if task_is_plan_n(t)
    )
    n = int(runtime._agent_expansions) + existing_plan_n + 1
    key = f"PLAN-{n}"
    tid = task_id(sid, key)
    if tid in runtime.tasks:
        return key
    now = runtime.clock.now()
    study = runtime.study
    ed = study.effective_deadline()
    deadline = min(now + timedelta(seconds=allocate), ed)
    spec = freeze_task_sandbox_limits(
        TaskSpec(
            task_key=key,
            title=f"Replan after {after_task}",
            goal="expand plan (M1 deterministic critic gate)",
            route=Route.PLANNER,
            required=False,
            depends_on=[after_task],
            criteria=list(PLAN_N_CRITIC_CRITERIA),
        ),
        runtime.catalog,
    )
    task = Task(
        task_id=tid,
        kind=TaskKind.PLAN,
        spec=spec,
        state=TaskState.PENDING,
        deadline=deadline,
        plan_role=PlanRole.PLAN_N,
    )
    payload = task.model_dump(mode="json")
    payload["replan_allocated_s"] = allocate
    payload["expand_after_task"] = after_task
    if pending is not None:
        payload["pending_expansion"] = pending.model_dump(mode="json")
    await runtime._append(  # noqa: SLF001
        [
            EventEnvelope(
                study_id=sid,
                type="TaskCreated",
                entity_key=tid,
                idem_key=make_idem("TaskCreated", tid, "expand_trigger"),
                payload=payload,
                ts=now,
                schema_version=TASK_CREATED_SCHEMA_VERSION,
                )
        ]
    )
    # replan_allocated_s is folded from TaskCreated in local_project (B3 / minor).
    reqs = runtime._expand_requests
    reqs[key] = {
        "after_task": after_task,
        "pending": pending.model_dump(mode="json") if pending is not None else None,
        "replan_allocated_s": allocate,
    }
    runtime._expand_pending.add(after_task)
    return key

async def maybe_expand_after_task_done(runtime: StudyRuntime, task_id_full: str) -> str | None:
    """H-g trigger: queue PLAN-n or run critic; never raise out of record/tick."""
    after = "?"
    try:
        task = runtime.tasks.get(task_id_full)
        if task is not None:
            after = task.spec.task_key
        return await _maybe_expand(runtime, task_id_full)
    except LeaseLost:
        raise
    except Exception as e:  # noqa: BLE001 — MJ-c: no silent loss
        await refuse_expansion(
            runtime,
            after,
            reason="invalid",
            detail=f"expand_exception:{type(e).__name__}"[:DETAIL_MAX_LEN],
        )
        return None

async def _maybe_expand(runtime: StudyRuntime, task_id_full: str) -> str | None:
    if runtime.study is None or runtime._finalized:
        return None
    task = runtime.tasks.get(task_id_full)
    if task is None or task.state != TaskState.DONE:
        return None
    # PLAN-n completion → M1 deterministic critic, then apply (or refuse).
    if task_is_plan_n(task):
        plan_key = task.spec.task_key
        if plan_n_already_closed(runtime, plan_key):
            return None
        if task.outcome == TaskOutcome.SUCCESS:
            from coscientist.core.plan.expand import apply_pending_expansion

            return await apply_pending_expansion(runtime, plan_key)
        # MJ-c: non-SUCCESS PLAN-n must not vanish.
        meta = runtime._expand_requests.get(plan_key)
        if not isinstance(meta, dict) or "after_task" not in meta:
            await refuse_expansion(
                runtime,
                plan_key,
                reason="invalid",
                detail="missing_expand_after_task",
                plan_key=plan_key,
            )
            return None
        raw_after = meta["after_task"]
        if not isinstance(raw_after, str) or not raw_after:
            await refuse_expansion(
                runtime,
                plan_key,
                reason="invalid",
                detail="invalid_expand_after_task",
                plan_key=plan_key,
            )
            return None
        after = raw_after
        oc = _outcome_token(task.outcome)
        await refuse_expansion(
            runtime,
            after,
            reason="critic_rejected",
            detail=f"plan_n_{oc}",
            plan_key=plan_key,
        )
        return None
    tkey = task.spec.task_key
    if expansion_already_handled(runtime, tkey):
        return None
    req = result_expansion_request(runtime, task_id_full)
    rc = _final_result_class(runtime, task_id_full)
    if req is not None and req.tasks:
        if task.outcome == TaskOutcome.SKIPPED:
            return None
        if task.outcome == TaskOutcome.FAILED and rc == "infra":
            return None
        if task.outcome not in (
            TaskOutcome.SUCCESS,
            TaskOutcome.PARTIAL,
            TaskOutcome.FAILED,
        ):
            return None
        try:
            err = await _prevalidate_request(runtime, req)
        except MaxDepthExceeded as e:
            await refuse_expansion(
                runtime,
                tkey,
                reason="expansion_limit",
                detail=str(e)[:DETAIL_MAX_LEN],
            )
            return None
        if err is not None:
            await refuse_expansion(runtime, tkey, reason="invalid", detail=err[:DETAIL_MAX_LEN])
            return None
        return await ensure_plan_n_task(runtime, after_task=tkey, pending=req)
    if tkey in expand_after_keys(runtime):
        return await ensure_plan_n_task(runtime, after_task=tkey, pending=None)
    return None

async def _prevalidate_request(runtime: StudyRuntime, req: ExpansionRequest) -> str | None:
    keyed = assign_core_keys(runtime, list(req.tasks))
    if isinstance(keyed, str):
        return keyed
    prev = active_plan_blob(runtime)
    prev_tasks = (
        [
            TaskSpec.model_validate(t) if isinstance(t, dict) else t
            for t in prev["tasks"]
        ]
        if prev is not None
        else []
    )
    known: dict[str, str] = {}
    if prev is not None:
        for q in prev["questions"]:
            if isinstance(q, dict):
                if "id" not in q:
                    return "plan_question_missing_id"
                qid = str(q["id"])
                if "text" not in q or not isinstance(q["text"], str):
                    return f"missing_question_text:{qid}"
                known[qid] = q["text"]
            else:
                known[str(q.id)] = q.text
    known.update({q.id: q.text for q in req.questions})
    # Propagate MaxDepthExceeded — caller maps to ExpansionRefused(expansion_limit).
    return validate_expansion(
        runtime,
        added_tasks=keyed,
        added_questions=list(req.questions),
        superseded_tasks=list(req.superseded_tasks),
        superseded_questions=list(req.superseded_questions),
        known_texts=known,
        prev_tasks=prev_tasks,
        active_q_ids=set(known),
    )

def plan_n_already_closed(runtime: StudyRuntime, plan_key: str) -> bool:
    """True when PlanExpanded / ExpansionRefused already closed this PLAN-n.

    Pending meta in ``_expand_requests`` means still open (do not treat the
    pending entry itself as «handled»).
    """
    for r in runtime._expansion_refusals:
        if r.get("plan_key") == plan_key:
            return True
    for p in runtime.plans.values():
        if p.get("plan_key") == plan_key:
            return True
    meta = runtime._expand_requests.get(plan_key)
    # Fold clears meta on expand/refuse. Meta still present → still open.
    return meta is None

async def resume_pending_expansions(runtime: StudyRuntime) -> int:
    """Re-run H-g after crash; skip PLAN-n already closed by expand/refuse."""
    if runtime.study is None or runtime._finalized:
        return 0
    n = 0
    for tid, task in list(runtime.tasks.items()):
        if task.state != TaskState.DONE:
            continue
        # PLAN-n: resume critic/apply or refuse non-SUCCESS.
        if task_is_plan_n(task):
            plan_key = task.spec.task_key
            if plan_n_already_closed(runtime, plan_key):
                continue
            meta = runtime._expand_requests.get(plan_key)
            if not isinstance(meta, dict) or "after_task" not in meta:
                await refuse_expansion(
                    runtime,
                    plan_key,
                    reason="invalid",
                    detail="missing_expand_after_task",
                    plan_key=plan_key,
                )
                continue
            raw_after = meta["after_task"]
            if not isinstance(raw_after, str) or not raw_after:
                await refuse_expansion(
                    runtime,
                    plan_key,
                    reason="invalid",
                    detail="invalid_expand_after_task",
                    plan_key=plan_key,
                )
                continue
            after = raw_after
            if task.outcome == TaskOutcome.SUCCESS:
                from coscientist.core.plan.expand import apply_pending_expansion

                await apply_pending_expansion(runtime, plan_key)
                n += 1
            else:
                oc = _outcome_token(task.outcome)
                await refuse_expansion(
                    runtime,
                    after,
                    reason="critic_rejected",
                    detail=f"plan_n_{oc}",
                    plan_key=plan_key,
                )
                n += 1
            continue
        reason = task.reason
        if reason is None:
            rv = None
        elif isinstance(reason, str):
            rv = reason
        else:
            rv = reason.value
        if rv == "superseded":
            continue
        tkey = task.spec.task_key
        if expansion_already_handled(runtime, tkey):
            continue
        req = result_expansion_request(runtime, tid)
        needs = (req is not None and bool(req.tasks)) or tkey in expand_after_keys(runtime)
        if not needs:
            continue
        await maybe_expand_after_task_done(runtime, tid)
        n += 1
    return n
