"""P2 share allocation / enforcement (M1 PR12) — split from shares.py for ≤500 LOC."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence

from coscientist.core.domain import Study, Task, TaskSpec, TaskState
from coscientist.core.shares import (
    _not_started_keys,
    _plan_specs_for_shares,
    critical_path_from,
    resolve_shares,
    shares_present,
)


def _raw_budget(spec: TaskSpec) -> float | None:
    v = getattr(spec, "budget_share", None)
    return None if v is None else float(v)


def renorm_deadline(
    task_key: str,
    *,
    study: Study,
    now: datetime,
    plan_tasks: Sequence[TaskSpec],
    not_started: set[str],
) -> datetime:
    """P2 deadline: share_i / Σ(CP∩not_started) × remaining (minus replan reserve)."""
    ed = study.effective_deadline()
    wall = max(0.0, (ed - now).total_seconds())
    reserve_frac = float(study.replan_reserve)
    remaining = wall * (1.0 - reserve_frac)
    shares = resolve_shares(plan_tasks, "deadline_share")
    share_i = float(shares.get(task_key, 0.0))
    cp = critical_path_from(task_key, plan_tasks)
    peers = [k for k in cp if k in not_started or k == task_key]
    if task_key not in peers:
        peers.append(task_key)
    denom = sum(float(shares.get(k, 0.0)) for k in peers)
    if denom <= 0.0 or remaining <= 0.0:
        return min(now, ed) if remaining <= 0.0 else min(now + timedelta(seconds=remaining), ed)
    span = remaining * (share_i / denom)
    return min(now + timedelta(seconds=span), ed)


def outstanding_task_allocations(runtime: Any) -> Decimal:
    """Sum of (alloc − spent) for non-DONE tasks with a journaled cap (open reservations)."""
    caps = getattr(runtime, "_task_budget_alloc", None) or {}
    spent_map = getattr(runtime, "_task_spent_rub", None) or {}
    tasks = getattr(runtime, "tasks", None) or {}
    total = Decimal("0")
    for tid, cap in caps.items():
        if cap is None:
            continue
        task = tasks.get(tid)
        if task is None or task.state == TaskState.DONE:
            continue
        used = Decimal(str(spent_map.get(tid, 0)))
        rem = Decimal(str(cap)) - used
        if rem > 0:
            total += rem
    return total


def allocate_budget(
    task_key: str,
    *,
    study: Study,
    spent_rub: Decimal,
    plan_tasks: Sequence[TaskSpec],
    not_started: set[str],
    outstanding: Decimal = Decimal("0"),
) -> Decimal | None:
    """P2 budget_i, or None when study has no max_cost_rub (no task cap).

    Cap 0 when pool exhausted or resolved share is 0 (caller refuses start).
    ``outstanding`` = allocated-but-unspent on other open tasks (B2).
    """
    bud = study.budget
    if bud is None or bud.max_cost_rub is None:
        return None
    max_cost = Decimal(str(bud.max_cost_rub))
    remaining = max_cost - Decimal(str(spent_rub))
    if remaining <= 0:
        return Decimal("0")
    reserve = remaining * Decimal(str(study.replan_reserve))
    pool = remaining - reserve - Decimal(str(outstanding))
    if pool <= 0:
        return Decimal("0")
    shares = resolve_shares(plan_tasks, "budget_share")
    share_i = Decimal(str(shares.get(task_key, 0.0)))
    if share_i <= 0:
        return Decimal("0")
    peers = set(not_started) | {task_key}
    denom = sum(Decimal(str(shares.get(k, 0.0))) for k in peers)
    if denom <= 0:
        return Decimal("0")
    return (pool * share_i / denom).quantize(Decimal("0.0001"))


def compute_p2_allocation(
    runtime: Any, task: Task
) -> tuple[datetime, Decimal | None, bool]:
    """Deadline, budget_rub|None, and whether deadline was renormed.

    Retries: caller must reuse a prior journaled cap (no re-allocation).
    """
    assert runtime.study is not None
    study: Study = runtime.study
    now = runtime.clock.now()
    ed = study.effective_deadline()
    plan_tasks = _plan_specs_for_shares(runtime)
    not_started = _not_started_keys(runtime.tasks)
    not_started.add(task.spec.task_key)
    do_renorm = shares_present(plan_tasks)
    if do_renorm:
        renormed = renorm_deadline(
            task.spec.task_key,
            study=study,
            now=now,
            plan_tasks=plan_tasks,
            not_started=not_started,
        )
        deadline = min(task.deadline, renormed, ed)
    else:
        deadline = min(task.deadline, ed)
    spent = Decimal(str(getattr(runtime, "_study_spent_rub", 0) or 0))
    outstanding = outstanding_task_allocations(runtime)
    # Do not count self if somehow already capped (retry path uses fixed cap).
    prior = (getattr(runtime, "_task_budget_alloc", None) or {}).get(task.task_id)
    if prior is not None:
        self_rem = max(
            Decimal("0"),
            Decimal(str(prior))
            - Decimal(
                str(
                    (getattr(runtime, "_task_spent_rub", None) or {}).get(task.task_id, 0)
                )
            ),
        )
        outstanding = max(Decimal("0"), outstanding - self_rem)
    budget = allocate_budget(
        task.spec.task_key,
        study=study,
        spent_rub=spent,
        plan_tasks=plan_tasks,
        not_started=not_started,
        outstanding=outstanding,
    )
    return deadline, budget, do_renorm


def p2_share_payload_fields(runtime: Any, task: Task) -> dict[str, Any]:
    """Fields merged into AttemptStarted for journaled share allocation.

    Reuses a fixed first-allocation cap across retries (M3).
    """
    prior = (getattr(runtime, "_task_budget_alloc", None) or {}).get(task.task_id)
    deadline, budget, renormed = compute_p2_allocation(runtime, task)
    if prior is not None:
        budget = Decimal(str(prior))
    return {
        "deadline": deadline.isoformat(),
        "allocated_budget_rub": None if budget is None else str(budget),
        "share_renorm": renormed,
    }


def agent_raises_budget_share(
    *,
    prev_tasks: Sequence[TaskSpec],
    added_tasks: Sequence[TaskSpec],
    after_task: str | None,
) -> str | None:
    """Reject agent expansions that raise their own (or total) budget_share."""
    prev_res = resolve_shares(prev_tasks, "budget_share")
    after_cap = float(prev_res.get(after_task or "", 0.0)) if after_task else None
    explicit_added = 0.0
    any_explicit = False
    for t in added_tasks:
        v = _raw_budget(t)
        if v is None:
            continue
        any_explicit = True
        if v < 0.0 or v > 1.0:
            return f"agent_share_raise:bad:{t.task_key}"
        explicit_added += v
    if not any_explicit:
        return None
    if after_cap is not None:
        if explicit_added > after_cap + 1e-9:
            return f"agent_share_raise:{after_task}:{explicit_added}>{after_cap}"
        for t in added_tasks:
            v = _raw_budget(t)
            if v is not None and v > after_cap + 1e-9:
                return f"agent_share_raise:{t.task_key}:{v}>{after_cap}"
        return None
    # after_task unknown: still forbid raising total explicit above previous Σ.
    prev_explicit = sum(
        v for v in (_raw_budget(t) for t in prev_tasks) if v is not None
    )
    if explicit_added > prev_explicit + 1e-9:
        return f"agent_share_raise:total:{explicit_added}>{prev_explicit}"
    return None


def ensure_share_defaults(spec: dict[str, Any]) -> dict[str, Any]:
    """Upcaster helper: old journals lack budget_share / deadline_share."""
    out = dict(spec)
    out.setdefault("budget_share", None)
    out.setdefault("deadline_share", None)
    return out


async def enforce_task_budget_share(runtime: Any, task_id: str) -> None:
    """If task spend > allocated share → SKIPPED(budget). Does not close study."""
    from coscientist.core.reasons import TaskReason

    caps = getattr(runtime, "_task_budget_alloc", None) or {}
    spent_map = getattr(runtime, "_task_spent_rub", None) or {}
    cap = caps.get(task_id)
    if cap is None:
        return
    spent = Decimal(str(spent_map.get(task_id, 0)))
    # Exceed only when spent > cap (equality is still within share).
    if spent <= Decimal(str(cap)):
        return
    task = (getattr(runtime, "tasks", None) or {}).get(task_id)
    if task is None or task.state == TaskState.DONE:
        return
    await runtime.cancel_task(
        task_id, reason=TaskReason.BUDGET, detail="task_share_exhausted"
    )


async def attempt_share_fields_or_refuse(runtime: Any, task: Task) -> dict[str, Any]:
    """P2 share payload, or cancel task and raise when cap is 0 (no attempt run)."""
    from coscientist.core.reasons import TaskReason

    fields = p2_share_payload_fields(runtime, task)
    alloc = fields.get("allocated_budget_rub")
    if alloc is not None and Decimal(str(alloc)) <= 0:
        await runtime.cancel_task(
            task.task_id, reason=TaskReason.BUDGET, detail="zero_share_or_pool"
        )
        raise RuntimeError("task budget share exhausted")
    return fields


def make_budget_debit_cb(runtime: Any) -> Any:
    """Executor.on_budget_debit → record_budget_debit + study exhaust check."""

    async def _cb(
        *,
        spent_rub: Decimal,
        request_id: str,
        task_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        await runtime.record_budget_debit(
            spent_rub=spent_rub, request_id=request_id, task_id=task_id, extra=extra
        )
        await runtime._emit_study_budget_if_exhausted()

    return _cb


def build_budget_shares_payload(
    runtime: Any,
    *,
    budget_shares: dict[str, Any],
    deadline_shares: dict[str, Any] | None = None,
    request_id: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Critic-validate HITL budget shares; return (error, payload) for journaling."""
    from coscientist.core.shares import critic_plan_shares

    plans = getattr(runtime, "plans", None) or {}
    if not plans:
        return "no_plan", None
    prev = max(plans.values(), key=lambda p: int(p.get("revision") or 0))
    raw_tasks = list(prev.get("tasks") or [])
    bmap = {str(k): float(v) for k, v in budget_shares.items()}
    dmap = {str(k): float(v) for k, v in (deadline_shares or {}).items()}
    specs: list[TaskSpec] = []
    for t in raw_tasks:
        spec = t if isinstance(t, TaskSpec) else TaskSpec.model_validate(t)
        key = spec.task_key
        upd: dict[str, Any] = {}
        if key in bmap:
            upd["budget_share"] = bmap[key]
        if key in dmap:
            upd["deadline_share"] = dmap[key]
        specs.append(spec.model_copy(update=upd) if upd else spec)
    err = critic_plan_shares(specs, study=getattr(runtime, "study", None))
    if err is not None:
        return err, None
    return None, {
        "request_id": request_id,
        "plan_id": str(prev.get("plan_id") or ""),
        "budget_shares": bmap,
        "deadline_shares": dmap,
        "tasks": [s.model_dump(mode="json") for s in specs],
    }


def fold_budget_shares_adjusted(runtime: Any, payload: dict[str, Any]) -> None:
    """Fold BudgetSharesAdjusted into active plan blob + live Task.spec (replay==live)."""
    tasks_payload = payload.get("tasks") or []
    if not tasks_payload:
        return
    specs = [
        t if isinstance(t, TaskSpec) else TaskSpec.model_validate(t) for t in tasks_payload
    ]
    by_key = {s.task_key: s for s in specs}
    plans = getattr(runtime, "plans", None) or {}
    pid = str(payload.get("plan_id") or "")
    target = plans.get(pid) if pid else None
    if target is None and plans:
        target = max(plans.values(), key=lambda p: int(p.get("revision") or 0))
    if isinstance(target, dict):
        target["tasks"] = [s.model_dump(mode="json") for s in specs]
    live = getattr(runtime, "tasks", None) or {}
    for tid, task in list(live.items()):
        ns = by_key.get(task.spec.task_key)
        if ns is not None:
            live[tid] = task.model_copy(update={"spec": ns})
