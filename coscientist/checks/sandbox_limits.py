"""Freeze per-check sandbox limits from the catalog only (PR17).

Agent-provided TaskSpec limit fields are stripped and noted. Limits are frozen
at every TaskCreated path (accept_plan / expand / foreach / expand_trigger) and
applied per ``template_id`` at evaluate time — retries cannot raise them.
"""

from __future__ import annotations

from typing import Any

from coscientist.core.execution.domain import Catalog, CodeCheck, TaskSpec


def _catalog_limits_for_criteria(
    catalog: Catalog | None,
    criteria: list[Any],
) -> dict[str, dict[str, Any]]:
    """``template_id`` → catalog timeout/memory/output/network for each CodeCheck."""
    if catalog is None:
        return {}
    by_id = {t.id: t for t in catalog.templates}
    out: dict[str, dict[str, Any]] = {}
    for ref in criteria or ():
        tid = str(getattr(ref, "template_id", None) or ref or "")
        if not tid or tid in out:
            continue
        tmpl = by_id.get(tid)
        if tmpl is None or not isinstance(tmpl.check, CodeCheck):
            continue
        entry: dict[str, Any] = {}
        if tmpl.check.timeout_s is not None:
            entry["timeout_s"] = float(tmpl.check.timeout_s)
        if tmpl.check.memory_mb is not None:
            entry["memory_mb"] = int(tmpl.check.memory_mb)
        if tmpl.check.max_output_bytes is not None:
            entry["max_output_bytes"] = int(tmpl.check.max_output_bytes)
        entry["allow_network"] = bool(tmpl.check.allow_network)
        out[tid] = entry
    return out


def freeze_task_sandbox_limits(spec: TaskSpec, catalog: Catalog | None) -> TaskSpec:
    """Strip agent limit fields; freeze per-check catalog limits onto TaskSpec.

    Always re-freezes from the catalog (ignores any prior agent-provided
    ``sandbox_timeout_s`` / ``sandbox_memory_mb`` / ``sandbox_check_limits``).
    """
    notes: list[str] = []
    if spec.sandbox_timeout_s is not None or spec.sandbox_memory_mb is not None:
        notes.append("stripped_agent_sandbox_limits")
    if spec.sandbox_check_limits:
        notes.append("stripped_agent_sandbox_check_limits")

    per_check = _catalog_limits_for_criteria(catalog, list(spec.criteria or []))
    return spec.model_copy(
        update={
            "sandbox_timeout_s": None,
            "sandbox_memory_mb": None,
            "sandbox_check_limits": per_check or None,
            "sandbox_limits_note": ";".join(notes) if notes else None,
        }
    )


def apply_frozen_limits(
    check: CodeCheck,
    spec: TaskSpec | None,
    *,
    template_id: str | None = None,
) -> CodeCheck:
    """Apply TaskSpec-frozen per-check limits (catalog values; no silent raise)."""
    if spec is None or not template_id:
        return check
    frozen = (spec.sandbox_check_limits or {}).get(str(template_id))
    if not isinstance(frozen, dict) or not frozen:
        return check
    updates: dict[str, Any] = {}
    if "timeout_s" in frozen and frozen["timeout_s"] is not None:
        updates["timeout_s"] = float(frozen["timeout_s"])
    if "memory_mb" in frozen and frozen["memory_mb"] is not None:
        updates["memory_mb"] = int(frozen["memory_mb"])
    if "max_output_bytes" in frozen and frozen["max_output_bytes"] is not None:
        updates["max_output_bytes"] = int(frozen["max_output_bytes"])
    if "allow_network" in frozen:
        updates["allow_network"] = bool(frozen["allow_network"])
    if not updates:
        return check
    return check.model_copy(update=updates)


# Back-compat alias used by older call sites / docs.
sandbox_limits_from_catalog = _catalog_limits_for_criteria
