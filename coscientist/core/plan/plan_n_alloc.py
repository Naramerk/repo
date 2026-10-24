"""Single PLAN-n / kind-key allocator (expand_trigger + expand_validate).

Key strings keep the ``{PREFIX}-{n}`` shape for humans; classification of
replan containers uses ``Task.plan_role``, never ``startswith("PLAN-")``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime

_KIND_INDEX = re.compile(r"^([A-Z]+)-(\d+)$")


def next_kind_index(
    runtime: StudyRuntime,
    prefix: str,
    extra_keys: Iterable[str] = (),
) -> int:
    """Next ``{prefix}-<n>`` index from runtime tasks + candidate keys."""
    n = 0
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    for rt_task in runtime.tasks.values():
        m = pat.match(rt_task.spec.task_key)
        if m:
            n = max(n, int(m.group(1)) + 1)
    for key in extra_keys:
        m = pat.match(str(key))
        if m:
            n = max(n, int(m.group(1)) + 1)
    return n


def allocate_plan_n_key(
    runtime: StudyRuntime,
    extra_keys: Iterable[str] = (),
) -> str:
    """Allocate the next ``PLAN-<n>`` key (sole writer of PLAN-n indices)."""
    return f"PLAN-{next_kind_index(runtime, 'PLAN', extra_keys)}"


def is_replan_container(task: Any) -> bool:
    """True when core stamped ``plan_role='replan'`` on a PLAN task."""
    from coscientist.core.execution.domain import TaskKind

    kind = getattr(task, "kind", None)
    if kind == TaskKind.PLAN:
        return getattr(task, "plan_role", None) == "replan"
    kind_val = getattr(kind, "value", kind)
    if kind_val == "plan":
        return getattr(task, "plan_role", None) == "replan"
    return False


def parse_kind_key(task_key: str) -> tuple[str, int] | None:
    """Return ``(prefix, n)`` for ``PREFIX-n`` keys; else None (no invent)."""
    if not isinstance(task_key, str) or not task_key:
        return None
    m = _KIND_INDEX.match(task_key)
    if not m:
        return None
    return m.group(1), int(m.group(2))
