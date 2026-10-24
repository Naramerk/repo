"""Task budget_share / deadline_share (M1 PR12 / DESIGN_M1 §5).

Pure helpers: resolve equal remainder, validate Σ and critical-path sums,
renormalize deadline/budget at P2, split parent share across foreach children.
Agents cannot raise their own budget_share — only accept_plan / HITL+critic.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence

from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.upcasters import require_exact_int
from coscientist.core.execution.domain import Study, Task, TaskSpec, TaskState

ShareField = Literal["budget_share", "deadline_share"]

BUDGET_EXHAUSTED_LINE = "не выполнено: бюджет исчерпан"
TASK_BUDGET_EXHAUSTED_LINE = "не выполнено: бюджет задачи исчерпан"
TASK_BUDGET_OVERRUN_LINE = (
    "выполнено с перерасходом доли бюджета задачи (лимит запрещает повторы)"
)
BUDGET_SHARES_REFUSED_LINE = "не выполнено: отказ критика (доли бюджета)"



def _coerce_share(value: object, *, what: str) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise TypeError(f"{what} must be int|float, got {type(value).__name__}")
    return float(value)  # audit-ok[coerce_float]: coerce after explicit numeric type gate


def _raw(spec: TaskSpec, field: ShareField) -> float | None:
    if field == "budget_share":
        v = spec.budget_share
    elif field == "deadline_share":
        v = spec.deadline_share
    else:
        raise ValueError(f"unknown share field: {field!r}")
    if v is None:
        return None
    return _coerce_share(v, what=f"TaskSpec.{field}")


def shares_present(tasks: Sequence[TaskSpec]) -> bool:
    """True when any task has an explicit budget_share or deadline_share."""
    return any(
        t.budget_share is not None or t.deadline_share is not None for t in tasks
    )


def resolve_shares(
    tasks: Sequence[TaskSpec], field: ShareField
) -> dict[str, float]:
    """Fill unspecified shares with equal parts of the undistributed remainder."""
    specs = list(tasks)
    if not specs:
        return {}
    explicit = {t.task_key: _raw(t, field) for t in specs}
    fixed_sum = sum(v for v in explicit.values() if v is not None)
    unset = [k for k, v in explicit.items() if v is None]
    rem = max(0.0, 1.0 - fixed_sum)
    if unset:
        each = rem / len(unset)
    else:
        each = 0.0
    out: dict[str, float] = {}
    for k, v in explicit.items():
        if v is not None:
            out[k] = v
        else:
            out[k] = each
    return out


def validate_budget_shares(
    tasks: Sequence[TaskSpec],
    *,
    replan_reserve: float,
    max_task_share: float,
) -> None:
    """Σ budget_share ≤ (1 − replan_reserve); per-task range and max_task_share."""
    specs = list(tasks)
    reserve = _coerce_share(replan_reserve, what="replan_reserve")
    if not (0.0 <= reserve <= 1.0):
        raise ValueError(f"replan_reserve out of range: {reserve}")
    sigma_cap = 1.0 - reserve
    max_share = _coerce_share(max_task_share, what="max_task_share")
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
    tasks: Sequence[TaskSpec],
    *,
    explicit_only: bool,
) -> float:
    """Max Σ deadline_share over any source→sink path in the DAG."""
    specs = list(tasks)
    if not specs:
        return 0.0
    if explicit_only:
        shares: dict[str, float] = {}
        for t in specs:
            v = _raw(t, "deadline_share")
            if v is None:
                shares[t.task_key] = 0.0
            else:
                shares[t.task_key] = v
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
        if node not in adj:
            raise JournalFormatError(f"deadline share DAG missing node {node!r}")
        nxt = adj[node]
        if node not in shares:
            raise JournalFormatError(f"deadline share map missing {node!r}")
        cur = acc + shares[node]
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
    replan_reserve: float,
) -> None:
    """Critical-path Σ **explicit** deadline_share ≤ (1 − replan_reserve)."""
    specs = list(tasks)
    reserve = _coerce_share(replan_reserve, what="replan_reserve")
    sigma_cap = 1.0 - reserve
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
    replan_reserve: float,
    max_task_share: float,
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
    if replan_reserve is not None:
        reserve = _coerce_share(replan_reserve, what="replan_reserve")
    elif study is not None:
        reserve = _coerce_share(study.replan_reserve, what="study.replan_reserve")
    else:
        return "missing_replan_reserve"
    if max_task_share is not None:
        mx = _coerce_share(max_task_share, what="max_task_share")
    elif study is not None:
        mx = _coerce_share(
            study.max_task_budget_share, what="study.max_task_budget_share"
        )
    else:
        return "missing_max_task_budget_share"
    try:
        validate_plan_shares(tasks, replan_reserve=reserve, max_task_share=mx)
    except ValueError as e:
        msg = str(e)
        if not msg:
            raise
        return msg
    return None


def split_parent_share(parent: float | None, n: int) -> list[float]:
    """Foreach children SPLIT the template's share (template / N).

    ``None`` template ⇒ refuse (caller must resolve default before split).
    """
    if n <= 0:
        return []
    if parent is None:
        raise ValueError("split_parent_share requires resolved parent share (not None)")
    base = _coerce_share(parent, what="parent_share")
    each = base / float(n)  # audit-ok[coerce_float]: coerce after explicit numeric type gate
    return [each] * n


def apply_foreach_parent_shares(
    template: TaskSpec,
    children: Sequence[TaskSpec],
    *,
    plan_tasks: Sequence[TaskSpec] | None = None,
    share_cap: float | None = None,
) -> list[TaskSpec]:
    """Stamp equal splits of the template's shares onto concrete children."""
    n = len(children)
    tb, td = template.budget_share, template.deadline_share
    if plan_tasks is not None:
        if tb is None:
            tb = resolve_shares(plan_tasks, "budget_share").get(template.task_key)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if td is None:
            td = resolve_shares(plan_tasks, "deadline_share").get(template.task_key)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if share_cap is not None:
        cap = _coerce_share(share_cap, what="share_cap")
        if tb is None:
            tb = cap
        elif template.budget_share is None:
            tb = min(_coerce_share(tb, what="budget_share"), cap)
        if td is None:
            td = cap
        elif template.deadline_share is None:
            td = min(_coerce_share(td, what="deadline_share"), cap)
    if tb is None or td is None:
        raise ValueError(
            "apply_foreach_parent_shares requires resolved budget/deadline shares"
        )
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
    try:
        plans = runtime.plans
    except AttributeError as e:
        raise JournalFormatError("runtime missing plans for share resolve") from e
    if plans is None:
        plans = {}
    if not isinstance(plans, dict):
        raise JournalFormatError(
            f"runtime.plans must be dict, got {type(plans).__name__}"
        )
    if plans:
        def _rev(p: dict[str, Any]) -> int:
            if "revision" not in p:
                raise JournalFormatError("plan blob missing revision")
            return require_exact_int(p["revision"], what="plan.revision")

        latest = max(plans.values(), key=_rev)
        if "tasks" not in latest:
            raise JournalFormatError("plan blob missing tasks")
        raw = latest["tasks"]
        if raw is None:
            raise JournalFormatError("plan blob tasks is None")
        if not isinstance(raw, list):
            raise JournalFormatError(
                f"plan blob tasks must be list, got {type(raw).__name__}"
            )
        specs: list[TaskSpec] = []
        for t in raw:
            if isinstance(t, TaskSpec):
                specs.append(t)
            elif isinstance(t, dict):
                specs.append(TaskSpec.model_validate(t))
            else:
                raise JournalFormatError(
                    f"plan task must be TaskSpec|dict, got {type(t).__name__}"
                )
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
        if node not in adj:
            raise JournalFormatError(f"deadline share DAG missing node {node!r}")
        nxt = [c for c in adj[node] if c not in stack]
        if not nxt:
            if acc > best_score:
                best_score = acc
                best_path = list(path)
            return
        for child in nxt:
            stack.add(child)
            path.append(child)
            if child not in shares:
                raise JournalFormatError(f"deadline share map missing {child!r}")
            dfs(child, path, acc + shares[child], stack)
            path.pop()
            stack.remove(child)

    if start_key not in shares:
        raise JournalFormatError(f"deadline share map missing {start_key!r}")
    dfs(start_key, [start_key], shares[start_key], {start_key})
    return set(best_path)


_ALLOC_EXPORTS = frozenset(
    {
        "allocate_budget",
        "agent_raises_budget_share",
        "attempt_share_fields_or_refuse",
        "budget_debit_event",
        "build_budget_shares_payload",
        "compute_p2_allocation",
        "enforce_task_budget_share",
        "fold_budget_shares_adjusted",
        "make_budget_debit_cb",
        "outstanding_task_allocations",
        "p2_share_payload_fields",
        "renorm_deadline",
        "would_exceed_budget_limit",
    }
)


def __getattr__(name: str) -> Any:
    if name in _ALLOC_EXPORTS:
        from coscientist.core.plan import share_alloc

        return getattr(share_alloc, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
