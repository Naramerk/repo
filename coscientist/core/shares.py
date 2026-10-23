"""Task budget_share / deadline_share (M1 PR12 / DESIGN_M1 §5).

Pure helpers: resolve equal remainder, validate Σ and critical-path sums,
renormalize deadline/budget at P2, split parent share across foreach children.
Agents cannot raise their own budget_share — only accept_plan / HITL+critic.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Mapping, Sequence

from coscientist.core.domain import Study, Task, TaskSpec, TaskState

ShareField = Literal["budget_share", "deadline_share"]

# Report line when a task or study closes for budget (Russian, DESIGN_M1 / finalize).
BUDGET_EXHAUSTED_LINE = "не выполнено: бюджет исчерпан"


def _raw(spec: TaskSpec, field: ShareField) -> float | None:
    v = getattr(spec, field, None)
    if v is None:
        return None
    return float(v)


def resolve_shares(
    tasks: Sequence[TaskSpec], field: ShareField
) -> dict[str, float]:
    """Fill unspecified shares with equal parts of the undistributed remainder.

    Explicit shares must already sum to ≤ 1 (caller validates). Remainder
    ``1 − Σ explicit`` is split equally among tasks with ``None``.
    """
    specs = list(tasks)
    if not specs:
        return {}
    explicit = {t.task_key: _raw(t, field) for t in specs}
    fixed_sum = sum(v for v in explicit.values() if v is not None)
    unset = [k for k, v in explicit.items() if v is None]
    rem = max(0.0, 1.0 - fixed_sum)
    each = (rem / len(unset)) if unset else 0.0
    out: dict[str, float] = {}
    for k, v in explicit.items():
        out[k] = float(v) if v is not None else each
    return out


def validate_budget_shares(tasks: Sequence[TaskSpec]) -> None:
    """Σ budget_share (explicit) ≤ 1 across the active revision."""
    specs = list(tasks)
    total = 0.0
    for t in specs:
        v = _raw(t, "budget_share")
        if v is None:
            continue
        if v < 0.0 or v > 1.0:
            raise ValueError(f"budget_share out of range for {t.task_key!r}: {v}")
        total += v
    if total > 1.0 + 1e-9:
        raise ValueError(f"Σ budget_share > 1 ({total})")


def _dag_adj(tasks: Sequence[TaskSpec]) -> dict[str, list[str]]:
    keys = {t.task_key for t in tasks}
    adj: dict[str, list[str]] = {t.task_key: [] for t in tasks}
    for t in tasks:
        for d in t.depends_on:
            if d in keys:
                adj[d].append(t.task_key)
    return adj


def critical_path_deadline_share_sum(tasks: Sequence[TaskSpec]) -> float:
    """Max Σ resolved deadline_share over any source→sink path in the DAG."""
    specs = list(tasks)
    if not specs:
        return 0.0
    shares = resolve_shares(specs, "deadline_share")
    keys = {t.task_key for t in specs}
    preds = {t.task_key: [d for d in t.depends_on if d in keys] for t in specs}
    adj = _dag_adj(specs)
    sources = [k for k, ps in preds.items() if not ps]
    if not sources:
        sources = list(keys)

    best = 0.0

    def dfs(node: str, acc: float, stack: set[str]) -> None:
        nonlocal best
        if node in stack:
            return
        stack.add(node)
        nxt = adj.get(node) or []
        cur = acc + shares.get(node, 0.0)
        if not nxt:
            if cur > best:
                best = cur
        else:
            for child in nxt:
                dfs(child, cur, stack)
        stack.remove(node)

    for s in sources:
        dfs(s, 0.0, set())
    return best


def validate_deadline_shares(tasks: Sequence[TaskSpec]) -> None:
    """Critical-path Σ deadline_share ≤ 1 (parallel branches do not add)."""
    specs = list(tasks)
    for t in specs:
        v = _raw(t, "deadline_share")
        if v is not None and (v < 0.0 or v > 1.0):
            raise ValueError(f"deadline_share out of range for {t.task_key!r}: {v}")
    # Also reject explicit sum > 1 on any single node set (path check covers chains).
    path = critical_path_deadline_share_sum(specs)
    if path > 1.0 + 1e-9:
        raise ValueError(f"critical-path Σ deadline_share > 1 ({path})")


def validate_plan_shares(tasks: Sequence[TaskSpec]) -> None:
    """accept_plan / expansion: budget Σ ≤ 1 and deadline critical-path ≤ 1."""
    validate_budget_shares(tasks)
    validate_deadline_shares(tasks)


def split_parent_share(parent: float | None, n: int) -> list[float | None]:
    """Foreach children share a parent's budget/deadline share equally.

    ``None`` parent → all children ``None`` (equal remainder at resolve time).
    ``n==0`` → [].
    """
    if n <= 0:
        return []
    if parent is None:
        return [None] * n
    each = float(parent) / float(n)
    return [each] * n


def apply_foreach_parent_shares(
    template: TaskSpec, children: Sequence[TaskSpec]
) -> list[TaskSpec]:
    """Stamp equal splits of the template's shares onto concrete children."""
    n = len(children)
    b_parts = split_parent_share(template.budget_share, n)
    d_parts = split_parent_share(template.deadline_share, n)
    out: list[TaskSpec] = []
    for i, child in enumerate(children):
        out.append(
            child.model_copy(
                update={
                    "budget_share": b_parts[i],
                    "deadline_share": d_parts[i],
                }
            )
        )
    return out


def _not_started_keys(runtime_tasks: Mapping[str, Task]) -> set[str]:
    """Task keys with no AttemptStarted yet (PENDING, zero attempts)."""
    out: set[str] = set()
    for t in runtime_tasks.values():
        if t.state == TaskState.DONE:
            continue
        if t.attempts:
            continue
        # Foreach templates are never dispatched.
        if t.spec.foreach is not None:
            continue
        out.add(t.spec.task_key)
    return out


def _plan_specs_for_shares(runtime: Any) -> list[TaskSpec]:
    """Active-revision TaskSpecs (prefer latest plan blob; else live tasks)."""
    plans = getattr(runtime, "plans", None) or {}
    if plans:
        latest = max(plans.values(), key=lambda p: int(p.get("revision") or 0))
        raw = latest.get("tasks") or []
        specs: list[TaskSpec] = []
        for t in raw:
            if isinstance(t, TaskSpec):
                specs.append(t)
            elif isinstance(t, dict):
                # Skip foreach templates still in the blob if superseded — keep all;
                # renorm filters by not-started runtime tasks.
                specs.append(TaskSpec.model_validate(t))
        if specs:
            return specs
    return [t.spec for t in runtime.tasks.values() if t.spec.foreach is None]


def critical_path_from(
    start_key: str, tasks: Sequence[TaskSpec]
) -> set[str]:
    """Nodes on some maximum-share path that includes ``start_key`` (forward)."""
    specs = list(tasks)
    shares = resolve_shares(specs, "deadline_share")
    keys = {t.task_key for t in specs}
    if start_key not in keys:
        return {start_key}
    adj = _dag_adj(specs)
    best_path: list[str] = [start_key]
    best_score = -1.0

    def dfs(node: str, path: list[str], acc: float, stack: set[str]) -> None:
        nonlocal best_path, best_score
        nxt = [c for c in (adj.get(node) or []) if c not in stack]
        if not nxt:
            if acc > best_score:
                best_score = acc
                best_path = list(path)
            return
        for child in nxt:
            stack.add(child)
            path.append(child)
            dfs(child, path, acc + shares.get(child, 0.0), stack)
            path.pop()
            stack.remove(child)

    dfs(start_key, [start_key], shares.get(start_key, 0.0), {start_key})
    return set(best_path)


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
    # Always include self even if already counted in not_started.
    if task_key not in peers:
        peers.append(task_key)
    denom = sum(float(shares.get(k, 0.0)) for k in peers)
    if denom <= 0.0 or remaining <= 0.0:
        return min(now, ed) if remaining <= 0.0 else min(now + timedelta(seconds=remaining), ed)
    span = remaining * (share_i / denom)
    return min(now + timedelta(seconds=span), ed)


def allocate_budget(
    task_key: str,
    *,
    study: Study,
    spent_rub: Decimal,
    plan_tasks: Sequence[TaskSpec],
    not_started: set[str],
) -> Decimal:
    """P2 budget_i = share_i / Σ(not_started) × (budget_remaining − budget_reserve)."""
    bud = study.budget
    if bud is None or bud.max_cost_rub is None:
        return Decimal("0")
    max_cost = Decimal(str(bud.max_cost_rub))
    remaining = max_cost - Decimal(str(spent_rub))
    if remaining <= 0:
        return Decimal("0")
    reserve = remaining * Decimal(str(study.replan_reserve))
    pool = remaining - reserve
    if pool <= 0:
        return Decimal("0")
    shares = resolve_shares(plan_tasks, "budget_share")
    share_i = Decimal(str(shares.get(task_key, 0.0)))
    peers = set(not_started) | {task_key}
    denom = sum(Decimal(str(shares.get(k, 0.0))) for k in peers)
    if denom <= 0:
        return Decimal("0")
    return (pool * share_i / denom).quantize(Decimal("0.0001"))


def compute_p2_allocation(
    runtime: Any, task: Task
) -> tuple[datetime, Decimal]:
    """Deadline + budget_rub for ``task`` at P2 (deterministic from folded state)."""
    assert runtime.study is not None
    study: Study = runtime.study
    now = runtime.clock.now()
    plan_tasks = _plan_specs_for_shares(runtime)
    not_started = _not_started_keys(runtime.tasks)
    # Current task is about to start — treat as still in the peer set.
    not_started.add(task.spec.task_key)
    deadline = renorm_deadline(
        task.spec.task_key,
        study=study,
        now=now,
        plan_tasks=plan_tasks,
        not_started=not_started,
    )
    spent = Decimal(str(getattr(runtime, "_study_spent_rub", 0) or 0))
    budget = allocate_budget(
        task.spec.task_key,
        study=study,
        spent_rub=spent,
        plan_tasks=plan_tasks,
        not_started=not_started,
    )
    return min(deadline, study.effective_deadline()), budget


def p2_share_payload_fields(runtime: Any, task: Task) -> dict[str, Any]:
    """Fields merged into AttemptStarted for journaled share renorm."""
    deadline, budget = compute_p2_allocation(runtime, task)
    return {
        "deadline": deadline.isoformat(),
        "allocated_budget_rub": str(budget),
        "share_renorm": True,
    }


def agent_raises_budget_share(
    *,
    prev_tasks: Sequence[TaskSpec],
    added_tasks: Sequence[TaskSpec],
    after_task: str | None,
) -> str | None:
    """Reject agent expansions that raise their own (or total) budget_share.

    Only accept_plan / HITL+critic may assign explicit shares that increase a
    lineage's allocation. Agents may leave shares ``None`` (equal remainder) or
    split ≤ the after_task's prior resolved share among added tasks.
    """
    prev_res = resolve_shares(prev_tasks, "budget_share")
    after_cap = float(prev_res.get(after_task or "", 0.0))
    explicit_added = 0.0
    any_explicit = False
    for t in added_tasks:
        v = _raw(t, "budget_share")
        if v is None:
            continue
        any_explicit = True
        if v < 0.0 or v > 1.0:
            return f"agent_share_raise:bad:{t.task_key}"
        explicit_added += v
    if not any_explicit:
        return None
    # Raising: explicit sum on added tasks exceeds the triggering task's share.
    if after_task and explicit_added > after_cap + 1e-9:
        return f"agent_share_raise:{after_task}:{explicit_added}>{after_cap}"
    # Or any single added share > after_cap when after_task known.
    if after_task:
        for t in added_tasks:
            v = _raw(t, "budget_share")
            if v is not None and v > after_cap + 1e-9:
                return f"agent_share_raise:{t.task_key}:{v}>{after_cap}"
    return None


def ensure_share_defaults(spec: dict[str, Any]) -> dict[str, Any]:
    """Upcaster helper: old journals lack budget_share / deadline_share."""
    out = dict(spec)
    out.setdefault("budget_share", None)
    out.setdefault("deadline_share", None)
    return out


async def enforce_task_budget_share(runtime: Any, task_id: str) -> None:
    """If task spend ≥ allocated share → SKIPPED(budget). Does not close study."""
    from coscientist.core.reasons import TaskReason

    caps = getattr(runtime, "_task_budget_alloc", None) or {}
    spent_map = getattr(runtime, "_task_spent_rub", None) or {}
    cap = caps.get(task_id)
    if cap is None:
        return
    spent = Decimal(str(spent_map.get(task_id, 0)))
    if spent < Decimal(str(cap)):
        return
    task = (getattr(runtime, "tasks", None) or {}).get(task_id)
    if task is None or task.state == TaskState.DONE:
        return
    await runtime.cancel_task(task_id, reason=TaskReason.BUDGET, detail="share_exhausted")
