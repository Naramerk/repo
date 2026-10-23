"""Freeze sandbox wall/CPU limits into TaskSpec at plan time (PR17)."""

from __future__ import annotations

from typing import Any

from coscientist.core.domain import Catalog, CodeCheck, TaskSpec


def sandbox_limits_from_catalog(
    catalog: Catalog | None,
    criteria: list[Any],
) -> tuple[float | None, int | None]:
    """Max timeout_s / memory_mb across catalog checks referenced by criteria."""
    if catalog is None:
        return None, None
    by_id = {t.id: t for t in catalog.templates}
    timeout: float | None = None
    memory: int | None = None
    for ref in criteria or ():
        tid = str(getattr(ref, "template_id", None) or ref or "")
        tmpl = by_id.get(tid)
        if tmpl is None or not isinstance(tmpl.check, CodeCheck):
            continue
        if tmpl.check.timeout_s is not None:
            t = float(tmpl.check.timeout_s)
            timeout = t if timeout is None else max(timeout, t)
        if tmpl.check.memory_mb is not None:
            m = int(tmpl.check.memory_mb)
            memory = m if memory is None else max(memory, m)
    return timeout, memory


def freeze_task_sandbox_limits(spec: TaskSpec, catalog: Catalog | None) -> TaskSpec:
    """Stamp sandbox limits onto TaskSpec if not already frozen (retries immutable)."""
    if spec.sandbox_timeout_s is not None or spec.sandbox_memory_mb is not None:
        return spec
    timeout, memory = sandbox_limits_from_catalog(catalog, list(spec.criteria or []))
    if timeout is None and memory is None:
        return spec
    return spec.model_copy(
        update={
            "sandbox_timeout_s": timeout,
            "sandbox_memory_mb": memory,
        }
    )


def apply_frozen_limits(check: CodeCheck, spec: TaskSpec | None) -> CodeCheck:
    """Prefer TaskSpec-frozen limits over live catalog values (no silent raise)."""
    if spec is None:
        return check
    updates: dict[str, Any] = {}
    if spec.sandbox_timeout_s is not None:
        updates["timeout_s"] = float(spec.sandbox_timeout_s)
    if spec.sandbox_memory_mb is not None:
        updates["memory_mb"] = int(spec.sandbox_memory_mb)
    if not updates:
        return check
    return check.model_copy(update=updates)
