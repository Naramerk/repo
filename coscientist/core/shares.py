"""Task budget_share / deadline_share (M1 PR12 / DESIGN_M1 §5).

Pure helpers: resolve equal remainder, validate Σ and critical-path sums,
renormalize deadline/budget at P2, split parent share across foreach children.
Agents cannot raise their own budget_share — only accept_plan / HITL+critic.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence

from coscientist.core.domain import Study, Task, TaskSpec, TaskState

ShareField = Literal["budget_share", "deadline_share"]

# Study-level vs per-task Russian report lines (finalize contributing_reasons).
BUDGET_EXHAUSTED_LINE = "не выполнено: бюджет исчерпан"
TASK_BUDGET_EXHAUSTED_LINE = "не выполнено: бюджет задачи исчерпан"
# Single-attempt spend past remaining share when runner cannot enforce mid-flight.
TASK_BUDGET_OVERRUN_LINE = (
    "не выполнено: перерасход доли бюджета задачи (лимит только запрещает повторы)"
)
BUDGET_SHARES_REFUSED_LINE = "не выполнено: отказ критика (доли бюджета)"

# Default per-task cap when Study.max_task_budget_share is unset.
DEFAULT_MAX_TASK_BUDGET_SHARE = 1.0


def _raw(spec: TaskSpec, field: ShareField) -> float | None:
    v = getattr(spec, field, None)
    if v is None:
        return None
    return float(v)


def shares_present(tasks: Sequence[TaskSpec]) -> bool:
    """True when any task has an explicit budget_share or deadline_share."""
    return any(
        t.budget_share is not None or t.deadline_share is not None for t in tasks
    )


def resolve_shares(
    tasks: Sequence[TaskSpec], field: ShareField
) -> dict[str, float]:
    """Fill unspecified shares with equal parts of the undistributed remainder.

    Explicit shares must already sum to ≤ cap (caller validates). Remainder
    ``cap − Σ explicit`` is split equally among tasks with ``None`` (cap=1 here;
    accept/critic may enforce a tighter Σ vs replan_reserve).
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


def validate_budget_shares(
    tasks: Sequence[TaskSpec],
    *,
    replan_reserve: float = 0.0,
    max_task_share: float = DEFAULT_MAX_TASK_BUDGET_SHARE,
) -> None:
    """Σ budget_share ≤ (1 − replan_reserve); per-task range and max_task_share."""
    specs = list(tasks)
    reserve = float(replan_reserve)
    if not (0.0 <= reserve <= 1.0):
        raise ValueError(f"replan_reserve out of range: {reserve}")
    sigma_cap = 1.0 - reserve
    max_share = float(max_task_share)
    total = 0.0
    for t in specs:
        v = _raw(t, "budget_share")
        if v is None:
            continue
        if v < 0.0 or v > 1.0:
            raise ValueError(f"budget_share out of range for {t.task_key!r}: {v}")
        if v > max_share + 1e-9:
            raise ValueError(
                f"budget_share above max_task_share for {t.task_key!r}: {v}>{max_share}"
            )
        total += v
    if total > sigma_cap + 1e-9:
        raise ValueError(f"Σ budget_share > {sigma_cap} (1−replan_reserve) ({total})")


def _dag_adj(tasks: Sequence[TaskSpec]) -> dict[str, list[str]]:
    keys = {t.task_key for t in tasks}
    adj: dict[str, list[str]] = {t.task_key: [] for t in tasks}
    for t in tasks:
        for d in t.depends_on:
            if d in keys:
                adj[d].append(t.task_key)
    return adj


def critical_path_deadline_share_sum(
    tasks: Sequence[TaskSpec], *, explicit_only: bool = False
) -> float:
    """Max Σ deadline_share over any source→sink path in the DAG.

    ``explicit_only``: None contributes 0 (plan critic vs reserve). Otherwise
    unresolved shares are filled via ``resolve_shares`` (allocation / tests).
    """
    specs = list(tasks)
    if not specs:
        return 0.0
    if explicit_only:
        shares = {}
        for t in specs:
            v = _raw(t, "deadline_share")
            shares[t.task_key] = 0.0 if v is None else float(v)
    else:
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


def validate_deadline_shares(
    tasks: Sequence[TaskSpec],
    *,
    replan_reserve: float = 0.0,
) -> None:
    """Critical-path Σ **explicit** deadline_share ≤ (1 − replan_reserve)."""
    specs = list(tasks)
    sigma_cap = 1.0 - float(replan_reserve)
    for t in specs:
        v = _raw(t, "deadline_share")
        if v is not None and (v < 0.0 or v > 1.0):
            raise ValueError(f"deadline_share out of range for {t.task_key!r}: {v}")
    path = critical_path_deadline_share_sum(specs, explicit_only=True)
    if path > sigma_cap + 1e-9:
        raise ValueError(
            f"critical-path Σ deadline_share > {sigma_cap} (1−replan_reserve) ({path})"
        )


def validate_plan_shares(
    tasks: Sequence[TaskSpec],
    *,
    replan_reserve: float = 0.0,
    max_task_share: float = DEFAULT_MAX_TASK_BUDGET_SHARE,
) -> None:
    """accept_plan / expansion / HITL: same deterministic critic gate for shares."""
    validate_budget_shares(
        tasks, replan_reserve=replan_reserve, max_task_share=max_task_share
    )
    validate_deadline_shares(tasks, replan_reserve=replan_reserve)


def critic_plan_shares(
    tasks: Sequence[TaskSpec],
    *,
    study: Study | None = None,
    replan_reserve: float | None = None,
    max_task_share: float | None = None,
) -> str | None:
    """Return error detail, or None if shares pass the plan critic."""
    reserve = (
        float(replan_reserve)
        if replan_reserve is not None
        else float(getattr(study, "replan_reserve", 0.0) or 0.0)
    )
    mx = (
        float(max_task_share)
        if max_task_share is not None
        else float(
            getattr(study, "max_task_budget_share", DEFAULT_MAX_TASK_BUDGET_SHARE)
            or DEFAULT_MAX_TASK_BUDGET_SHARE
        )
    )
    try:
        validate_plan_shares(tasks, replan_reserve=reserve, max_task_share=mx)
    except ValueError as e:
        return str(e) or "invalid_shares"
    return None


def split_parent_share(parent: float | None, n: int) -> list[float]:
    """Foreach children SPLIT the template's share (template / N).

    ``None`` template ⇒ one default share (1.0) split among N children (explicit).
    """
    if n <= 0:
        return []
    base = 1.0 if parent is None else float(parent)
    each = base / float(n)
    return [each] * n


def apply_foreach_parent_shares(
    template: TaskSpec,
    children: Sequence[TaskSpec],
    *,
    plan_tasks: Sequence[TaskSpec] | None = None,
    share_cap: float | None = None,
) -> list[TaskSpec]:
    """Stamp equal splits of the template's shares onto concrete children.

    ``None`` template share ⇒ one resolved default from ``plan_tasks`` (else 1.0),
    capped by ``share_cap`` (typically ``1 − replan_reserve``) so Σ still passes critic.
    """
    n = len(children)
    tb, td = template.budget_share, template.deadline_share
    if plan_tasks is not None:
        if tb is None:
            tb = resolve_shares(plan_tasks, "budget_share").get(template.task_key)
        if td is None:
            td = resolve_shares(plan_tasks, "deadline_share").get(template.task_key)
    if share_cap is not None:
        if tb is None:
            tb = float(share_cap)
        elif template.budget_share is None:
            tb = min(float(tb), float(share_cap))
        if td is None:
            td = float(share_cap)
        elif template.deadline_share is None:
            td = min(float(td), float(share_cap))
    b_parts = split_parent_share(tb, n)
    d_parts = split_parent_share(td, n)
    return [
        child.model_copy(
            update={"budget_share": b_parts[i], "deadline_share": d_parts[i]}
        )
        for i, child in enumerate(children)
    ]


def _not_started_keys(runtime_tasks: Mapping[str, Task]) -> set[str]:
    """Task keys with no AttemptStarted yet (PENDING, zero attempts)."""
    out: set[str] = set()
    for t in runtime_tasks.values():
        if t.state == TaskState.DONE:
            continue
        if t.attempts:
            continue
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
                specs.append(TaskSpec.model_validate(t))
        if specs:
            return specs
    return [t.spec for t in runtime.tasks.values() if t.spec.foreach is None]


def critical_path_from(start_key: str, tasks: Sequence[TaskSpec]) -> set[str]:
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



# Lazy re-export allocation API (avoids circular import with share_alloc).
_ALLOC_EXPORTS = frozenset(
    {
        "allocate_budget",
        "agent_raises_budget_share",
        "attempt_share_fields_or_refuse",
        "budget_debit_event",
        "build_budget_shares_payload",
        "compute_p2_allocation",
        "enforce_task_budget_share",
        "ensure_share_defaults",
        "fold_budget_shares_adjusted",
        "make_budget_debit_cb",
        "outstanding_task_allocations",
        "p2_share_payload_fields",
        "renorm_deadline",
    }
)


def __getattr__(name: str) -> Any:
    if name in _ALLOC_EXPORTS:
        from coscientist.core import share_alloc

        return getattr(share_alloc, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
