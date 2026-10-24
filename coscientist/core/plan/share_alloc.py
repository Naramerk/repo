"""P2 share allocation / enforcement (M1 PR12) — split from shares.py for ≤500 LOC."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence

from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.domain import Study, Task, TaskSpec, TaskState
from coscientist.core.plan.shares import (
    _not_started_keys,
    _plan_specs_for_shares,
    critical_path_from,
    resolve_shares,
    shares_present,
)


def _as_decimal(value: object, *, what: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if type(value) is bool or not isinstance(value, (int, float, str)):
        raise JournalFormatError(f"{what} must be Decimal-compatible, got {type(value).__name__}")
    return Decimal(str(value))


def _spent_for_cap(spent_map: dict[str, Any], tid: str) -> Decimal:
    try:
        return _as_decimal(spent_map[tid], what=f"task spent {tid!r}")
    except KeyError as e:
        raise JournalFormatError(
            f"task spent missing for capped task {tid!r}"
        ) from e


def _raw_budget(spec: TaskSpec) -> float | None:
    return spec.budget_share


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
    reserve_frac = study.replan_reserve
    remaining = wall * (1.0 - reserve_frac)
    shares = resolve_shares(plan_tasks, "deadline_share")
    share_i = shares[task_key]
    cp = critical_path_from(task_key, plan_tasks)
    peers = [k for k in cp if k in not_started or k == task_key]
    if task_key not in peers:
        peers.append(task_key)
    denom = sum(shares[k] for k in peers)
    if denom <= 0.0 or remaining <= 0.0:
        return min(now, ed) if remaining <= 0.0 else min(now + timedelta(seconds=remaining), ed)  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
    span = remaining * (share_i / denom)
    return min(now + timedelta(seconds=span), ed)


def outstanding_task_allocations(runtime: Any) -> Decimal:
    """Sum of (alloc − spent) for non-DONE tasks with a journaled cap (open reservations)."""
    caps = runtime._task_budget_alloc
    spent_map = runtime._task_spent_rub
    tasks = runtime.tasks
    total = Decimal("0")
    for tid, cap in caps.items():
        if cap is None:
            continue
        task = tasks.get(tid)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if task is None or task.state == TaskState.DONE:
            continue
        used = _spent_for_cap(spent_map, tid)
        rem = _as_decimal(cap, what=f"task alloc {tid!r}") - used
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
    outstanding: Decimal = Decimal("0"),  # audit-ok[named_default]: named constant / enum member default; not silent invent
) -> Decimal | None:
    """P2 budget_i, or None when study has no max_cost_rub (no task cap).

    Cap 0 when pool exhausted or resolved share is 0 (caller refuses start).
    ``outstanding`` = allocated-but-unspent on other open tasks (B2).
    """
    bud = study.budget
    if bud is None or bud.max_cost_rub is None:
        return None
    # Stage / non-plan keys (SPEC, REPORT, …) are outside the share pool — no task cap.
    plan_keys = {t.task_key for t in plan_tasks}
    if task_key not in plan_keys:
        return None
    max_cost = _as_decimal(bud.max_cost_rub, what="study.max_cost_rub")
    remaining = max_cost - spent_rub
    if remaining <= 0:
        return Decimal("0")
    reserve = remaining * _as_decimal(study.replan_reserve, what="study.replan_reserve")
    pool = remaining - reserve - outstanding
    if pool <= 0:
        return Decimal("0")
    shares = resolve_shares(plan_tasks, "budget_share")
    share_i = _as_decimal(shares[task_key], what=f"budget_share[{task_key}]")
    if share_i <= 0:
        return Decimal("0")
    # Only plan keys appear in ``shares``; stage/non-plan not_started keys are out of pool.
    peers = (set(not_started) | {task_key}) & set(shares)
    denom = sum(_as_decimal(shares[k], what=f"budget_share[{k}]") for k in peers)
    if denom <= 0:
        return Decimal("0")
    return (pool * share_i / denom).quantize(Decimal("0.0001"))


def compute_p2_allocation(
    runtime: Any, task: Task
) -> tuple[datetime, Decimal | None, bool]:
    """Deadline, budget_rub|None, and whether deadline was renormed.

    Retries: caller must reuse a prior journaled cap (no re-allocation).
    """
    if runtime.study is None:
        raise RuntimeError("P2 allocation requires an open study")
    study: Study = runtime.study
    now = runtime.clock.now()
    ed = study.effective_deadline()
    plan_tasks = _plan_specs_for_shares(runtime)
    not_started = _not_started_keys(runtime.tasks)
    not_started.add(task.spec.task_key)
    plan_keys = {t.task_key for t in plan_tasks}
    # Renorm only for plan tasks when shares are present (stages keep wall deadline).
    do_renorm = shares_present(plan_tasks) and task.spec.task_key in plan_keys
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
    spent = _as_decimal(runtime._study_spent_rub, what="study_spent_rub")
    outstanding = outstanding_task_allocations(runtime)
    # Do not count self if somehow already capped (retry path uses fixed cap).
    caps = runtime._task_budget_alloc
    spent_map = runtime._task_spent_rub
    prior = caps.get(task.task_id)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if prior is not None:
        self_rem = max(
            Decimal("0"),
            _as_decimal(prior, what="prior alloc") - _spent_for_cap(spent_map, task.task_id),
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
    ``budget_limit_rub`` is the *remaining* task cap for this attempt (runner
    limit). M1 has no LLM/tool loop — mid-flight enforcement is an M1 gap;
    use ``would_exceed_budget_limit`` in M2. Until then overrun is recorded
    post-hoc and the cap only forbids retries.
    """
    caps = runtime._task_budget_alloc
    spent_map = runtime._task_spent_rub
    prior = caps.get(task.task_id)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    deadline, budget, renormed = compute_p2_allocation(runtime, task)
    if prior is not None:
        budget = _as_decimal(prior, what="prior alloc")
        spent = _spent_for_cap(spent_map, task.task_id)
    elif task.task_id in spent_map:
        spent = _spent_for_cap(spent_map, task.task_id)
    else:
        # No prior cap and never billed — zero spend is the semantic, not a map default.
        spent = Decimal("0")
    if budget is None:
        limit: Decimal | None = None
    else:
        limit = max(Decimal("0"), _as_decimal(budget, what="budget") - spent)
    return {
        "deadline": deadline.isoformat(),
        "allocated_budget_rub": None if budget is None else str(budget),  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
        "budget_limit_rub": None if limit is None else str(limit),  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
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
    if after_task is None:
        after_cap = None
    elif after_task in prev_res:
        after_cap = prev_res[after_task]
    else:
        after_cap = 0.0
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


async def enforce_task_budget_share(runtime: Any, task_id: str) -> None:
    """If task spend > allocated share → SKIPPED(budget). Does not close study."""
    from coscientist.core.claims.reasons import TaskReason

    caps = runtime._task_budget_alloc
    spent_map = runtime._task_spent_rub
    if task_id not in caps:
        return
    cap = caps[task_id]
    if cap is None:
        return
    spent = _spent_for_cap(spent_map, task_id)
    # Exceed only when spent > cap (equality is still within share).
    if spent <= _as_decimal(cap, what=f"task alloc {task_id!r}"):
        return
    task = runtime.tasks.get(task_id)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if task is None or task.state == TaskState.DONE:
        return
    await runtime.cancel_task(
        task_id, reason=TaskReason.BUDGET, detail="task_share_exhausted"
    )


async def attempt_share_fields_or_refuse(
    runtime: Any, task: Task
) -> dict[str, Any] | None:
    """P2 share payload, or cancel task and return None when cap is 0.

    Honest refusal (no exception): caller must not start an attempt so the
    task/study can still close with a report (StudyFinalized invariant).
    """
    from coscientist.core.claims.reasons import TaskReason

    fields = p2_share_payload_fields(runtime, task)
    alloc = fields.get("allocated_budget_rub")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if alloc is not None and _as_decimal(alloc, what="allocated_budget_rub") <= 0:
        await runtime.cancel_task(
            task.task_id, reason=TaskReason.BUDGET, detail="zero_share_or_pool"
        )
        return None
    return fields


def would_exceed_budget_limit(
    *,
    spent_so_far: Decimal | float | int | str,
    next_cost: Decimal | float | int | str,
    limit: Decimal | float | int | str | None,
) -> bool:
    """True when a runner must abort *before* the next billed call.

    M1 has no LLM/tool-call loop — this helper is the contract for M2 runners.
    Core still records post-hoc overrun when the runner cannot enforce mid-flight.
    """
    if limit is None:
        return False
    return (
        _as_decimal(spent_so_far, what="spent_so_far")
        + _as_decimal(next_cost, what="next_cost")
        > _as_decimal(limit, what="budget_limit")
    )


def budget_debit_event(
    *,
    study_id: str,
    spent_rub: Decimal | float | int | str,
    request_id: str,
    task_id: str | None,
    ts: datetime,
    extra: dict[str, Any] | None = None,
) -> Any | None:
    """Build ``BudgetDebited`` for the same append batch as the attempt result."""
    from coscientist.core.events.events import UnstampedEvent, make_idem

    spent = _as_decimal(spent_rub, what="spent_rub")
    if spent <= 0:
        return None
    payload: dict[str, Any] = {
        "spent_rub": str(spent),
        "request_id": request_id,
    }
    if extra is not None:
        payload.update(extra)
    if task_id:
        payload["task_id"] = task_id
    actual_rub = payload["actual_rub"] if "actual_rub" in payload else spent  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
    return UnstampedEvent(
        study_id=study_id,
        type="BudgetDebited",
        entity_key=request_id,
        idem_key=make_idem(
            "BudgetDebited",
            request_id,
            str(spent),
            str(actual_rub),
        ),
        payload=payload,
        ts=ts,
        schema_version=writer_schema_version('BudgetDebited'),
    )


def make_budget_debit_cb(runtime: Any) -> Any:
    """Post-append hook after atomic ``BudgetDebited`` (enforce + study exhaust).

    The debit event itself is built by the executor and appended in the same
    batch as ``ResultRecorded`` / ``TaskDone``. This callback only runs side
    effects after the fold.
    """

    async def _cb(
        *,
        spent_rub: Decimal,
        request_id: str,
        task_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        _ = spent_rub, request_id, extra
        if task_id:
            from coscientist.core.plan.shares import enforce_task_budget_share

            await enforce_task_budget_share(runtime, task_id)
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
    from coscientist.core.plan.shares import critic_plan_shares

    plans = runtime.plans
    if not plans:
        return "no_plan", None
    def _revision(p: dict[str, Any]) -> int:
        raw = p.get("revision")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if raw is None:
            raise JournalFormatError("plan revision missing")
        if type(raw) is bool or not isinstance(raw, int):
            raise JournalFormatError(f"plan revision must be int, got {type(raw).__name__}")
        return raw

    prev = max(plans.values(), key=_revision)
    raw_tasks = prev.get("tasks")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if raw_tasks is None:
        raise JournalFormatError("plan tasks missing")
    if not isinstance(raw_tasks, list):
        raise JournalFormatError("plan tasks must be a list")
    bmap = {}
    for k, v in budget_shares.items():
        if not isinstance(k, str):
            raise JournalFormatError(f"budget_shares key must be str, got {type(k).__name__}")
        if type(v) is bool or not isinstance(v, (int, float)):
            raise JournalFormatError(f"budget_shares[{k!r}] must be numeric")
        bmap[k] = float(v)  # audit-ok[coerce_float]: coerce after int|float type gate
    dmap = {}
    if deadline_shares is not None:
        for k, v in deadline_shares.items():
            if not isinstance(k, str):
                raise JournalFormatError(
                    f"deadline_shares key must be str, got {type(k).__name__}"
                )
            if type(v) is bool or not isinstance(v, (int, float)):
                raise JournalFormatError(f"deadline_shares[{k!r}] must be numeric")
            dmap[k] = float(v)  # audit-ok[coerce_float]: coerce after int|float type gate
    specs = []
    for t in raw_tasks:
        spec = t if isinstance(t, TaskSpec) else TaskSpec.model_validate(t)  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
        key = spec.task_key
        upd = {}
        if key in bmap:
            upd["budget_share"] = bmap[key]
        if key in dmap:
            upd["deadline_share"] = dmap[key]
        specs.append(spec.model_copy(update=upd) if upd else spec)  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
    err = critic_plan_shares(specs, study=runtime.study)
    if err is not None:
        return err, None
    plan_id = prev.get("plan_id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if plan_id is None:
        raise JournalFormatError("plan_id missing")
    if not isinstance(plan_id, str):
        raise JournalFormatError(f"plan_id must be str, got {type(plan_id).__name__}")
    return None, {
        "request_id": request_id,
        "plan_id": plan_id,
        "budget_shares": bmap,
        "deadline_shares": dmap,
        "tasks": [s.model_dump(mode="json") for s in specs],
    }


def fold_budget_shares_adjusted(runtime: Any, payload: dict[str, Any]) -> None:
    """Fold BudgetSharesAdjusted into active plan blob + live Task.spec (replay==live)."""
    tasks_payload = payload.get("tasks")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if tasks_payload is None:
        return
    if not isinstance(tasks_payload, list):
        raise JournalFormatError("BudgetSharesAdjusted.tasks must be a list")
    if not tasks_payload:
        return
    specs = [
        t if isinstance(t, TaskSpec) else TaskSpec.model_validate(t) for t in tasks_payload  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
    ]
    by_key = {s.task_key: s for s in specs}
    plans = runtime.plans
    pid_raw = payload.get("plan_id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if isinstance(pid_raw, str) and pid_raw:
        target = plans.get(pid_raw)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    else:
        target = None
    if target is None and plans:
        def _revision(p: dict[str, Any]) -> int:
            raw = p.get("revision")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if raw is None:
                raise JournalFormatError("plan revision missing")
            if type(raw) is bool or not isinstance(raw, int):
                raise JournalFormatError(
                    f"plan revision must be int, got {type(raw).__name__}"
                )
            return raw

        target = max(plans.values(), key=_revision)
    if isinstance(target, dict):
        target["tasks"] = [s.model_dump(mode="json") for s in specs]
    live = runtime.tasks
    for tid, task in list(live.items()):
        ns = by_key.get(task.spec.task_key)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if ns is not None:
            live[tid] = task.model_copy(update={"spec": ns})
