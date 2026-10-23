"""H-g expand trigger after TaskDone + ExpansionRefused (M1 PR9).

Agent ``expansion_request`` / ``expand_after`` enqueue a PLAN-n container with
explicit critic criteria. Direct ``PlanExpanded`` is applied after the M1
deterministic critic gate (or HITL) via ``expand_plan`` / ``apply_pending_expansion``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from coscientist.core.domain import (
    ExpansionRequest,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.expand_critic import PLAN_N_CRITIC_CRITERIA
from coscientist.core.ids import task_id

if TYPE_CHECKING:
    from coscientist.core.machine import StudyRuntime


def expand_after_keys(runtime: StudyRuntime) -> set[str]:
    keys: set[str] = set()
    for plan in runtime.plans.values():
        for k in plan.get("expand_after") or []:
            if k:
                keys.add(str(k))
    return keys


def _final_attempt_id(runtime: StudyRuntime, tid: str) -> str | None:
    task = runtime.tasks.get(tid)
    if task is None or not task.attempts:
        return None
    return task.attempts[-1]


def _result_row_for_attempt(runtime: StudyRuntime, aid: str) -> dict[str, Any]:
    return dict(runtime.results.get(aid) or runtime.results.get(f"result:{aid}") or {})


def result_expansion_request(runtime: StudyRuntime, tid: str) -> ExpansionRequest | None:
    """Read ExpansionRequest from the FINAL attempt only (MJ3)."""
    aid = _final_attempt_id(runtime, tid)
    if not aid:
        return None
    row = _result_row_for_attempt(runtime, aid)
    raw = row.get("expansion_request")
    if isinstance(raw, dict):
        try:
            return ExpansionRequest.model_validate(raw)
        except Exception:  # noqa: BLE001 — totality
            return None
    return None


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
        if str(p.get("after_task") or "") == after_task:
            return True
    for r in getattr(runtime, "_expansion_refusals", []) or []:
        if str(r.get("after_task") or "") == after_task:
            return True
    pending = getattr(runtime, "_expand_requests", None) or {}
    if isinstance(pending, dict):
        for meta in pending.values():
            if isinstance(meta, dict) and str(meta.get("after_task") or "") == after_task:
                return True
    return False


async def refuse_expansion(
    runtime: StudyRuntime,
    after_task: str,
    *,
    reason: str,
    detail: str,
) -> None:
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return
    sid = runtime.study.study_id
    now = runtime.clock.now()
    await runtime._append(  # noqa: SLF001
        [
            EventEnvelope(
                study_id=sid,
                type="ExpansionRefused",
                entity_key=f"{sid}:{after_task}:{reason}:{detail}",
                idem_key=make_idem("ExpansionRefused", sid, after_task, reason, detail),
                payload={
                    "after_task": after_task,
                    "reason": reason,
                    "detail": detail,
                },
                ts=now,
                schema_version=1,
            )
        ]
    )


def _agent_quota_ok(runtime: StudyRuntime) -> bool:
    study = runtime.study
    if study is None:
        return False
    return int(getattr(runtime, "_agent_expansions", 0)) < int(study.max_expansions)


def _replan_allocate_s(runtime: StudyRuntime) -> float:
    study = runtime.study
    if study is None:
        return 0.0
    now = runtime.clock.now()
    used = float(getattr(runtime, "_replan_used_s", 0.0))
    budget = study.replan_budget_s() - used
    ed = study.effective_deadline()
    return min(
        float(study.plan_timeout_s),
        max(0.0, budget),
        max(0.0, (ed - now).total_seconds()),
    )


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
        1
        for t in runtime.tasks.values()
        if t.kind == TaskKind.PLAN and str(t.spec.task_key).startswith("PLAN-")
    )
    n = int(getattr(runtime, "_agent_expansions", 0)) + existing_plan_n + 1
    key = f"PLAN-{n}"
    tid = task_id(sid, key)
    if tid in runtime.tasks:
        return key
    now = runtime.clock.now()
    study = runtime.study
    ed = study.effective_deadline()
    deadline = min(now + timedelta(seconds=allocate), ed)
    spec = TaskSpec(
        task_key=key,
        title=f"Replan after {after_task}",
        goal="expand plan (M1 deterministic critic gate)",
        route="planner",
        required=False,
        depends_on=[after_task],
        criteria=list(PLAN_N_CRITIC_CRITERIA),
    )
    task = Task(
        task_id=tid,
        kind=TaskKind.PLAN,
        spec=spec,
        state=TaskState.PENDING,
        deadline=deadline,
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
                schema_version=6,
            )
        ]
    )
    # replan_allocated_s is folded from TaskCreated in local_project (B3 / minor).
    reqs = getattr(runtime, "_expand_requests", None)
    if not isinstance(reqs, dict):
        runtime._expand_requests = {}  # noqa: SLF001
        reqs = runtime._expand_requests
    reqs[key] = {
        "after_task": after_task,
        "pending": pending.model_dump(mode="json") if pending is not None else None,
        "replan_allocated_s": allocate,
    }
    pending_set = getattr(runtime, "_expand_pending", None)
    if isinstance(pending_set, set):
        pending_set.add(after_task)
    else:
        runtime._expand_pending = {after_task}  # noqa: SLF001
    return key


async def maybe_expand_after_task_done(runtime: StudyRuntime, task_id_full: str) -> str | None:
    """H-g trigger: queue PLAN-n or run critic; never raise out of record/tick."""
    after = "?"
    try:
        task = runtime.tasks.get(task_id_full)
        if task is not None:
            after = task.spec.task_key
        return await _maybe_expand(runtime, task_id_full)
    except Exception as e:  # noqa: BLE001 — MJ-c: no silent loss
        await refuse_expansion(
            runtime,
            after,
            reason="invalid",
            detail=f"expand_exception:{type(e).__name__}"[:200],
        )
        return None


async def _maybe_expand(runtime: StudyRuntime, task_id_full: str) -> str | None:
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return None
    task = runtime.tasks.get(task_id_full)
    if task is None or task.state != TaskState.DONE:
        return None
    # PLAN-n completion → M1 deterministic critic, then apply (or refuse).
    if task.kind == TaskKind.PLAN and task.spec.task_key.startswith("PLAN-"):
        if task.outcome == TaskOutcome.SUCCESS:
            from coscientist.core.expand import apply_pending_expansion

            return await apply_pending_expansion(runtime, task.spec.task_key)
        # MJ-c: non-SUCCESS PLAN-n must not vanish.
        meta = (getattr(runtime, "_expand_requests", None) or {}).get(task.spec.task_key)
        after = str((meta or {}).get("after_task") or task.spec.task_key)
        oc = getattr(task.outcome, "value", task.outcome) or "unknown"
        await refuse_expansion(
            runtime, after, reason="critic_rejected", detail=f"plan_n_{oc}"
        )
        if isinstance(getattr(runtime, "_expand_requests", None), dict):
            runtime._expand_requests.pop(task.spec.task_key, None)  # noqa: SLF001
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
        err = await _prevalidate_request(runtime, req)
        if err is not None:
            await refuse_expansion(runtime, tkey, reason="invalid", detail=err[:200])
            return None
        return await ensure_plan_n_task(runtime, after_task=tkey, pending=req)
    if tkey in expand_after_keys(runtime):
        return await ensure_plan_n_task(runtime, after_task=tkey, pending=None)
    return None


async def _prevalidate_request(runtime: StudyRuntime, req: ExpansionRequest) -> str | None:
    from coscientist.core.domain import TaskSpec
    from coscientist.core.expand_validate import (
        active_plan_blob,
        assign_core_keys,
        validate_expansion,
    )

    keyed = assign_core_keys(runtime, list(req.tasks))
    if isinstance(keyed, str):
        return keyed
    prev = active_plan_blob(runtime) or {}
    prev_tasks = [
        TaskSpec.model_validate(t) if isinstance(t, dict) else t for t in (prev.get("tasks") or [])
    ]
    known: dict[str, str] = {}
    for q in prev.get("questions") or []:
        if isinstance(q, dict) and q.get("id"):
            known[str(q["id"])] = str(q.get("text") or "")
    known.update({q.id: q.text for q in req.questions})
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


async def resume_pending_expansions(runtime: StudyRuntime) -> int:
    """Re-run H-g after crash; PLAN-n states must not be lost silently (MJ-c)."""
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return 0
    n = 0
    for tid, task in list(runtime.tasks.items()):
        if task.state != TaskState.DONE:
            continue
        # PLAN-n: resume critic/apply or refuse non-SUCCESS.
        if task.kind == TaskKind.PLAN and task.spec.task_key.startswith("PLAN-"):
            meta = (getattr(runtime, "_expand_requests", None) or {}).get(task.spec.task_key)
            after = str((meta or {}).get("after_task") or task.spec.task_key)
            if expansion_already_handled(runtime, after) and task.outcome == TaskOutcome.SUCCESS:
                # Already expanded or refused for after_task.
                if not meta:
                    continue
            if task.outcome == TaskOutcome.SUCCESS and meta:
                from coscientist.core.expand import apply_pending_expansion

                await apply_pending_expansion(runtime, task.spec.task_key)
                n += 1
            elif task.outcome != TaskOutcome.SUCCESS and meta:
                oc = getattr(task.outcome, "value", task.outcome) or "unknown"
                await refuse_expansion(
                    runtime, after, reason="critic_rejected", detail=f"plan_n_{oc}_recover"
                )
                runtime._expand_requests.pop(task.spec.task_key, None)  # noqa: SLF001
                n += 1
            continue
        reason = getattr(task, "reason", None)
        rv = getattr(reason, "value", reason)
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
