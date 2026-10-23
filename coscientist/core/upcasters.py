"""schema_version upcasters: immutable old payloads; read via upcasters[type][v] -> v+1."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from coscientist.core.reasons import CloseReason, TaskReason

Upcaster = Callable[[dict[str, Any]], dict[str, Any]]

# type -> version -> function that upgrades payload from version to version+1
UPCASTERS: dict[str, dict[int, Upcaster]] = {}


def register_upcaster(event_type: str, from_version: int, fn: Upcaster) -> None:
    UPCASTERS.setdefault(event_type, {})[from_version] = fn


def upcast(event_type: str, schema_version: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Apply chained upcasters until no more are registered."""
    version = schema_version
    data = dict(payload)
    while True:
        fn = UPCASTERS.get(event_type, {}).get(version)
        if fn is None:
            break
        data = fn(data)
        version += 1
    return version, data


def _task_created_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out.setdefault("inherited_from", None)
    return out


def _ensure_spec_addresses(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        spec = dict(spec)
        spec.setdefault("addresses", [])
        out["spec"] = spec
    return out


def _task_created_v3_to_v4_input_mode(payload: dict[str, Any]) -> dict[str, Any]:
    """Pre-PR5 InputSpec.mode default was use_partial; keep that on replay."""
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        spec = dict(spec)
        inputs = spec.get("inputs")
        if isinstance(inputs, list):
            rewritten: list[Any] = []
            for item in inputs:
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("mode", "use_partial")
                    rewritten.append(item)
                else:
                    rewritten.append(item)
            spec["inputs"] = rewritten
        out["spec"] = spec
    return out


def _task_created_v4_to_v5_input_kind(payload: dict[str, Any]) -> dict[str, Any]:
    """PR7: InputSpec.kind defaults to artifact; ref absent."""
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        spec = dict(spec)
        inputs = spec.get("inputs")
        if isinstance(inputs, list):
            rewritten: list[Any] = []
            for item in inputs:
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("kind", "artifact")
                    item.setdefault("ref", None)
                    rewritten.append(item)
                else:
                    rewritten.append(item)
            spec["inputs"] = rewritten
        out["spec"] = spec
    return out


def _strip_plan_criterion_roles(criteria: list[Any]) -> list[Any]:
    """Drop plan-side role; catalog template is the sole authority at evaluate time."""
    rewritten: list[Any] = []
    for item in criteria:
        if isinstance(item, dict):
            item = dict(item)
            item.pop("role", None)
            rewritten.append(item)
        else:
            rewritten.append(item)
    return rewritten


def _task_created_v5_to_v6_check_role(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8: strip plan criterion role (resolve from catalog by template_id)."""
    out = dict(payload)
    spec = out.get("spec")
    if isinstance(spec, dict):
        spec = dict(spec)
        criteria = spec.get("criteria")
        if isinstance(criteria, list):
            spec["criteria"] = _strip_plan_criterion_roles(criteria)
        out["spec"] = spec
    return out


def _plan_accepted_v2_to_v3_check_role(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8: strip PlanAccepted.tasks[].criteria role (catalog is source of truth)."""
    out = dict(payload)
    tasks = out.get("tasks")
    if isinstance(tasks, list):
        rewritten: list[Any] = []
        for t in tasks:
            if isinstance(t, dict):
                t = dict(t)
                criteria = t.get("criteria")
                if isinstance(criteria, list):
                    t["criteria"] = _strip_plan_criterion_roles(criteria)
                rewritten.append(t)
            else:
                rewritten.append(t)
        out["tasks"] = rewritten
    return out


def _plan_accepted_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out.setdefault("questions", [])
    tasks = out.get("tasks")
    if isinstance(tasks, list):
        rewritten: list[Any] = []
        for t in tasks:
            if isinstance(t, dict):
                t = dict(t)
                t.setdefault("addresses", [])
                rewritten.append(t)
            else:
                rewritten.append(t)
        out["tasks"] = rewritten
    return out


def _study_created_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """Old journals had no finalize_reserve_s; default 0 keeps pre-reserve deadlines."""
    out = dict(payload)
    out.setdefault("finalize_reserve_s", 0)
    return out


def _study_created_v2_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    """PR 6: k_ref / refutation_priority defaults for old StudyCreated journals."""
    out = dict(payload)
    out.setdefault("refutations_needed", 1)
    out.setdefault("refutation_priority", False)
    return out


def _study_created_v3_to_v4(payload: dict[str, Any]) -> dict[str, Any]:
    """PR 8: confirmations_needed (k) as its own schema bump (not piggybacked on v3)."""
    out = dict(payload)
    out.setdefault("confirmations_needed", 1)
    return out


def _study_created_v4_to_v5(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8b early: aggregate / evalue_alpha (e-value deferred; keep fields for replay)."""
    out = dict(payload)
    out.setdefault("aggregate", "count")
    out.setdefault("evalue_alpha", 0.05)
    # Old journals that stored evalue still replay as count (no softer verdict).
    if out.get("aggregate") == "evalue":
        out["aggregate"] = "count"
    return out


def _study_created_v5_to_v6(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8b: supported_needed (≥2); confirmations_needed stays PR8 snapshot k."""
    out = dict(payload)
    out.setdefault("supported_needed", 2)
    try:
        if int(out.get("supported_needed", 2)) < 2:
            out["supported_needed"] = 2
    except (TypeError, ValueError):
        out["supported_needed"] = 2
    return out


def _study_created_v6_to_v7(payload: dict[str, Any]) -> dict[str, Any]:
    """PR9: expansion quotas + replan_reserve (max_expansions ← max_replans)."""
    out = dict(payload)
    if "max_expansions" not in out:
        # Migration: max_expansions replaces max_replans when present.
        if "max_replans" in out:
            try:
                out["max_expansions"] = int(out["max_replans"])
            except (TypeError, ValueError):
                out["max_expansions"] = 2
        else:
            out["max_expansions"] = 2
    out.setdefault("max_human_expansions", 10)
    out.setdefault("replan_reserve", 0.1)
    out.setdefault("max_tasks", 64)
    out.setdefault("max_depth", 8)
    out.setdefault("plan_timeout_s", 600)
    return out


def _plan_accepted_v3_to_v4_expand_after(payload: dict[str, Any]) -> dict[str, Any]:
    """PR9: PlanAccepted.expand_after defaults to []."""
    out = dict(payload)
    out.setdefault("expand_after", [])
    return out


def _study_finalized_v2_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8b: minimal_report.hypotheses[] (empty for legacy)."""
    out = dict(payload)
    mr = out.get("minimal_report")
    if isinstance(mr, dict):
        mr = dict(mr)
        mr.setdefault("hypotheses", [])
        mr.setdefault("hypotheses_unavailable", None)
        out["minimal_report"] = mr
    out.setdefault("hypothesis_report", out.get("hypothesis_report") or [])
    out.setdefault("hypotheses_unavailable", None)
    return out


def _coerce_task_reason_field(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    reason = out.get("reason")
    if reason is not None:
        try:
            out["reason"] = TaskReason(reason).value
        except ValueError as e:
            raise ValueError(f"unknown TaskDone.reason: {reason!r}") from e
    return out


def _coerce_close_reason_field(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    close_reason = out.get("close_reason")
    if close_reason is not None:
        try:
            out["close_reason"] = CloseReason(close_reason).value
        except ValueError as e:
            raise ValueError(f"unknown AttemptClosed.close_reason: {close_reason!r}") from e
    return out


def _rename_simulated_provenance(payload: dict[str, Any]) -> dict[str, Any]:
    """Rename legacy provenance/source simulated→stub and verdict simulated→stubbed."""
    out = dict(payload)
    if out.get("provenance") == "simulated":
        out["provenance"] = "stub"
    if out.get("source") == "simulated":
        out["source"] = "stub"
    if out.get("verdict") == "simulated":
        out["verdict"] = "stubbed"
    inputs = out.get("inputs")
    if isinstance(inputs, list):
        rewritten: list[Any] = []
        for item in inputs:
            if isinstance(item, dict) and item.get("source") == "simulated":
                rewritten.append({**item, "source": "stub"})
            else:
                rewritten.append(item)
        out["inputs"] = rewritten
    return out


def _study_finalized_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """Add refusal + structured question counts on minimal_report (PR 4)."""
    out = dict(payload)
    out.setdefault("refusal", None)
    mr = out.get("minimal_report")
    if isinstance(mr, dict):
        mr = dict(mr)
        mr.setdefault("refusal", out.get("refusal"))
        qs = mr.get("questions") or []
        if not isinstance(qs, list):
            qs = []
        mr.setdefault("question_count", len(qs))
        if "not_attempted_count" not in mr:
            mr["not_attempted_count"] = sum(
                1 for q in qs if isinstance(q, dict) and q.get("disposition") == "not_attempted"
            )
        out["minimal_report"] = mr
    return out


def _attempt_started_v2_to_v3_inputs(payload: dict[str, Any]) -> dict[str, Any]:
    """PR7: P2 InputBinding list (+ digest) on AttemptStarted; old journals → []."""
    out = dict(payload)
    out.setdefault("inputs", [])
    return out


register_upcaster("TaskCreated", 1, _task_created_v1_to_v2)
register_upcaster("TaskCreated", 2, _ensure_spec_addresses)
register_upcaster("TaskCreated", 3, _task_created_v3_to_v4_input_mode)
register_upcaster("TaskCreated", 4, _task_created_v4_to_v5_input_kind)
register_upcaster("TaskCreated", 5, _task_created_v5_to_v6_check_role)
register_upcaster("PlanAccepted", 1, _plan_accepted_v1_to_v2)
register_upcaster("PlanAccepted", 2, _plan_accepted_v2_to_v3_check_role)
register_upcaster("PlanAccepted", 3, _plan_accepted_v3_to_v4_expand_after)
register_upcaster("StudyCreated", 1, _study_created_v1_to_v2)
register_upcaster("StudyCreated", 2, _study_created_v2_to_v3)
register_upcaster("StudyCreated", 3, _study_created_v3_to_v4)
register_upcaster("StudyCreated", 4, _study_created_v4_to_v5)
register_upcaster("StudyCreated", 5, _study_created_v5_to_v6)
register_upcaster("StudyCreated", 6, _study_created_v6_to_v7)
register_upcaster("TaskDone", 1, _coerce_task_reason_field)
register_upcaster("AttemptClosed", 1, _coerce_close_reason_field)
register_upcaster("AttemptStarted", 1, _rename_simulated_provenance)
register_upcaster("AttemptStarted", 2, _attempt_started_v2_to_v3_inputs)


def _result_recorded_v2_to_v3_output_kinds(payload: dict[str, Any]) -> dict[str, Any]:
    """PR8b: ``output_kinds`` (catalog code|data); absent → {}."""
    out = dict(payload)
    out.setdefault("output_kinds", {})
    return out


register_upcaster("ResultRecorded", 1, _rename_simulated_provenance)
register_upcaster("ResultRecorded", 2, _result_recorded_v2_to_v3_output_kinds)
register_upcaster("StudyFinalized", 1, _study_finalized_v1_to_v2)
register_upcaster("StudyFinalized", 2, _study_finalized_v2_to_v3)
