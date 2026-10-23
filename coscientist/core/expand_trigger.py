"""H-g expand trigger after TaskDone + ExpansionRefused (M1 PR9)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from coscientist.core.domain import (
    ExpansionRequest,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
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


def result_expansion_request(runtime: StudyRuntime, tid: str) -> ExpansionRequest | None:
    """Read ExpansionRequest from the latest ResultRecorded for this task."""
    task = runtime.tasks.get(tid)
    aids = list(reversed(list(task.attempts))) if task is not None else []
    for aid in aids:
        row = runtime.results.get(aid) or {}
        raw = row.get("expansion_request")
        if isinstance(raw, dict):
            try:
                return ExpansionRequest.model_validate(raw)
            except Exception:  # noqa: BLE001 — totality
                return None
    for row in runtime.results.values():
        if row.get("task_id") == tid and isinstance(row.get("expansion_request"), dict):
            try:
                return ExpansionRequest.model_validate(row["expansion_request"])
            except Exception:  # noqa: BLE001
                return None
    return None


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
                entity_key=f"{sid}:{after_task}:{detail}",
                idem_key=make_idem("ExpansionRefused", sid, after_task, detail, str(now.timestamp())),
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


async def ensure_plan_n_task(runtime: StudyRuntime, *, after_task: str) -> None:
    """Create PLAN-<n> container for planner→critic attempts (DESIGN §3.3)."""
    if runtime.study is None:
        return
    sid = runtime.study.study_id
    n = int(getattr(runtime, "_agent_expansions", 0)) + 1
    key = f"PLAN-{n}"
    tid = task_id(sid, key)
    if tid in runtime.tasks:
        return
    now = runtime.clock.now()
    study = runtime.study
    used = float(getattr(runtime, "_replan_used_s", 0.0))
    budget = study.replan_budget_s() - used
    ed = study.effective_deadline()
    allocate = min(
        float(study.plan_timeout_s),
        max(0.0, budget),
        max(0.0, (ed - now).total_seconds()),
    )
    if allocate <= 0:
        await refuse_expansion(runtime, after_task, reason="expansion_limit", detail="replan_reserve")
        return
    deadline = min(now + timedelta(seconds=allocate), ed)
    spec = TaskSpec(
        task_key=key,
        title=f"Replan after {after_task}",
        goal="expand plan",
        route="planner",
        required=False,
        depends_on=[after_task],
    )
    task = Task(
        task_id=tid,
        kind=TaskKind.PLAN,
        spec=spec,
        state=TaskState.PENDING,
        deadline=deadline,
    )
    await runtime._append(  # noqa: SLF001
        [
            EventEnvelope(
                study_id=sid,
                type="TaskCreated",
                entity_key=tid,
                idem_key=make_idem("TaskCreated", tid, "expand_trigger"),
                payload=task.model_dump(mode="json"),
                ts=now,
                schema_version=6,
            )
        ]
    )
    runtime._replan_used_s = used + allocate  # noqa: SLF001


async def maybe_expand_after_task_done(runtime: StudyRuntime, task_id_full: str) -> str | None:
    """H-g trigger after TaskDone: expansion_request or expand_after."""
    from coscientist.core.expand import expand_plan

    if runtime.study is None or getattr(runtime, "_finalized", False):
        return None
    task = runtime.tasks.get(task_id_full)
    if task is None or task.state != TaskState.DONE:
        return None
    tkey = task.spec.task_key
    req = result_expansion_request(runtime, task_id_full)
    # Apply typed expansion_request on any non-SKIPPED DONE (incl. FAILED).
    if req is not None and req.tasks and task.outcome in (TaskOutcome.SUCCESS, TaskOutcome.PARTIAL, TaskOutcome.FAILED):
        return await expand_plan(
            runtime,
            after_task=tkey,
            added_tasks=list(req.tasks),
            added_questions=list(req.questions),
            superseded_tasks=list(req.superseded_tasks),
            superseded_questions=list(req.superseded_questions),
            origin="emergent",
            reason=req.reason or "expansion_request",
            delegated_by=None,
        )
    if tkey in expand_after_keys(runtime):
        pending = getattr(runtime, "_expand_pending", None)
        if isinstance(pending, set):
            pending.add(tkey)
        else:
            runtime._expand_pending = {tkey}  # noqa: SLF001
        await ensure_plan_n_task(runtime, after_task=tkey)
    return None
