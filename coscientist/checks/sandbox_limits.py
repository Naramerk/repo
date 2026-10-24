"""Freeze per-check sandbox limits from the catalog only (PR17 / #41).

Agent-provided TaskSpec limit fields are stripped and noted. Limits are frozen
at the accept_plan / expand / foreach TaskCreated paths (create_study core tasks
and expand_trigger PLAN-n are #39) and applied per ``template_id`` at evaluate time — retries cannot raise them.
``allow_network`` / ``allow_cgroup_degraded`` come only from the frozen catalog
record; missing freeze → typed refuse (never the raw CodeCheck values).

``RequirementRef`` forbids ``exempt_from_freeze`` (``extra=forbid``).
Catalog-authored ``RequirementTemplate.exempt_from_freeze`` skips freeze.
PLAN-n critic markers are exactly
``frozenset(r.template_id for r in PLAN_N_CRITIC_CRITERIA)`` — membership, not
a ``startswith`` heuristic. ``Task.plan_role`` in #38 replaces the skip set.
``catalog is None`` → ``CatalogError`` (no soft empty freeze).

``apply_frozen_limits`` requires a freeze record for sandbox-backed
``CodeCheck`` kinds (``catalog.EXECUTABLE_CODE_KINDS``); other kinds never run in
the jail and pass through. ``ReviewCheck`` must not call apply (evaluate routes reviews separately).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from coscientist.checks.catalog import (
    EXECUTABLE_CODE_KINDS,
    EXPERIMENT_DEFAULTS_KEY,
    FROZEN_LIMIT_KEYS,
    PROPOSED_CHECK_TEMPLATE_ID,
)
from coscientist.core.events.errors import CatalogError
from coscientist.core.execution.domain import (
    Catalog,
    CodeCheck,
    FrozenCheckLimits,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.plan.expand_critic import PLAN_N_CRITIC_CRITERIA

# Exact PLAN-n critic template ids (same source as expand_critic criteria).
PLAN_N_CRITIC_TEMPLATE_IDS: frozenset[str] = frozenset(
    r.template_id for r in PLAN_N_CRITIC_CRITERIA
)


def is_plan_n_critic_template_id(tid: str) -> bool:
    """True iff ``tid`` is one of ``PLAN_N_CRITIC_CRITERIA`` template ids."""
    return isinstance(tid, str) and tid in PLAN_N_CRITIC_TEMPLATE_IDS


def _frozen_row(tid: str, check: CodeCheck) -> FrozenCheckLimits:
    """Catalog ``CodeCheck`` → typed frozen row (values copied, nothing derived)."""
    an = check.allow_network
    ad = check.allow_cgroup_degraded
    if not isinstance(an, bool) or not isinstance(ad, bool):
        raise CatalogError(
            f"sandbox_limits_bad_security_toggle:{tid}:"
            f"allow_network={type(an).__name__},"
            f"allow_cgroup_degraded={type(ad).__name__}"
        )
    try:
        return FrozenCheckLimits(
            timeout_s=check.timeout_s,
            memory_mb=check.memory_mb,
            max_output_bytes=check.max_output_bytes,
            pids_max=check.pids_max,
            rlimit_nproc=check.rlimit_nproc,
            reap_timeout_s=check.reap_timeout_s,
            output_drain_join_s=check.output_drain_join_s,
            allow_network=an,
            allow_cgroup_degraded=ad,
        )
    except ValidationError as e:
        raise CatalogError(f"sandbox_limits_bad_row:{tid}:{e}") from e


def _catalog_limits_for_criteria(
    catalog: Catalog | None,
    criteria: list[RequirementRef],
) -> dict[str, FrozenCheckLimits]:
    """``template_id`` → typed frozen row for each non-exempt CodeCheck."""
    if catalog is None:
        raise CatalogError("sandbox_limits_catalog_required")
    by_id = {t.id: t for t in catalog.templates}
    tids: list[str] = []
    for ref in criteria:
        if not isinstance(ref, RequirementRef):
            raise CatalogError(
                f"sandbox_limits_bad_criterion:{type(ref).__name__}"
            )
        tid = ref.template_id
        if not isinstance(tid, str) or not tid or tid != tid.strip():
            raise CatalogError("sandbox_limits_empty_template_id")
        tids.append(tid)
    # PLAN-n markers: checks-owned scope exclusion until #38 Task.plan_role.
    # Duplicates collapse (first occurrence wins; criteria precede extras).
    scoped = [
        tid
        for tid in dict.fromkeys(tids)
        if not is_plan_n_critic_template_id(tid)
    ]
    out: dict[str, FrozenCheckLimits] = {}
    for tid in scoped:
        if tid not in by_id:
            raise CatalogError(f"sandbox_limits_unknown_template:{tid}")
        tmpl = by_id[tid]
        # Catalog-authored exemption: never frozen, never applied.
        if not tmpl.exempt_from_freeze:
            if not isinstance(tmpl.check, CodeCheck):
                raise CatalogError(
                    f"sandbox_limits_non_codecheck:{tid}:"
                    f"{type(tmpl.check).__name__}"
                )
            out[tid] = _frozen_row(tid, tmpl.check)
    return out


def freeze_task_sandbox_limits(spec: TaskSpec, catalog: Catalog | None) -> TaskSpec:
    """Strip agent limit fields; freeze per-check catalog limits onto TaskSpec.

    Always re-freezes from the catalog (ignores any prior agent-provided
    ``sandbox_timeout_s`` / ``sandbox_memory_mb`` / ``sandbox_check_limits``).

    Also freezes:
    * ``proposed_check`` when present in the catalog (approved proposals apply it)
    * ``catalog.defaults["experiment"]`` when ``criteria`` is empty (evaluate
      injects those defaults; freeze must already hold them)
    """
    notes: list[str] = []
    if spec.sandbox_timeout_s is not None or spec.sandbox_memory_mb is not None:
        notes.append("stripped_agent_sandbox_limits")
    if spec.sandbox_check_limits:
        notes.append("stripped_agent_sandbox_check_limits")

    refs = list(spec.criteria)
    extra: list[RequirementRef] = []
    if catalog is not None:
        by_id = {t.id: t for t in catalog.templates}
        if PROPOSED_CHECK_TEMPLATE_ID in by_id:
            extra.append(RequirementRef(template_id=PROPOSED_CHECK_TEMPLATE_ID))
        # Match evaluate_task_checks empty-criteria default injection.
        if not refs and EXPERIMENT_DEFAULTS_KEY in catalog.defaults:
            for tid in catalog.defaults[EXPERIMENT_DEFAULTS_KEY]:
                if not isinstance(tid, str) or not tid:
                    raise CatalogError("sandbox_limits_bad_default_template_id")
                extra.append(RequirementRef(template_id=tid))
    # Criteria first so explicit rows win over extras on duplicate tids.
    per_check = _catalog_limits_for_criteria(catalog, [*refs, *extra])
    return spec.model_copy(
        update={
            "sandbox_timeout_s": None,
            "sandbox_memory_mb": None,
            "sandbox_check_limits": per_check,
            "sandbox_limits_note": ";".join(notes) if notes else None,
        }
    )


def apply_frozen_limits(
    check: CodeCheck,
    spec: TaskSpec | None,
    *,
    template_id: str | None = None,
) -> CodeCheck:
    """Apply TaskSpec-frozen per-check limits (catalog values; no silent raise).

    Sandbox-backed kinds (``EXECUTABLE_CODE_KINDS``) always require a freeze
    record — missing ``spec`` / ``template_id`` / frozen entry → ``CatalogError``.
    Non-sandbox ``CodeCheck`` kinds never run in the jail, so limits are not
    applicable and the check is returned unchanged (core-stage tasks created
    outside the freeze paths are deferred to #39). ``ReviewCheck`` must not
    call this (evaluate routes reviews without apply).
    """
    if check.kind not in EXECUTABLE_CODE_KINDS:
        return check
    if template_id is not None and not isinstance(template_id, str):
        raise CatalogError(
            f"sandbox_limits_bad_template_id:{type(template_id).__name__}"
        )
    if spec is None or not template_id:
        raise CatalogError(
            "sandbox_limits_unfrozen:missing_spec_or_template_id"
        )
    limits_map = spec.sandbox_check_limits
    if limits_map is None or template_id not in limits_map:
        raise CatalogError(f"sandbox_limits_unfrozen:{template_id}")
    frozen = limits_map[template_id]
    if not isinstance(frozen, FrozenCheckLimits):
        raise CatalogError(f"sandbox_limits_unfrozen:{template_id}")
    # model_validate re-runs CodeCheck validators on the typed row values.
    updates: dict[str, Any] = {
        key: getattr(frozen, key) for key in FROZEN_LIMIT_KEYS
    }
    updates["allow_network"] = frozen.allow_network
    updates["allow_cgroup_degraded"] = frozen.allow_cgroup_degraded
    try:
        return CodeCheck.model_validate(
            {**check.model_dump(mode="python"), **updates}
        )
    except ValidationError as e:
        raise CatalogError(f"sandbox_limits_bad_frozen:{template_id}:{e}") from e
