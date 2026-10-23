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


def upcast(
    event_type: str, schema_version: int, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
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
                1
                for q in qs
                if isinstance(q, dict) and q.get("disposition") == "not_attempted"
            )
        out["minimal_report"] = mr
    return out


register_upcaster("TaskCreated", 1, _task_created_v1_to_v2)
register_upcaster("TaskCreated", 2, _ensure_spec_addresses)
register_upcaster("PlanAccepted", 1, _plan_accepted_v1_to_v2)
register_upcaster("StudyCreated", 1, _study_created_v1_to_v2)
register_upcaster("TaskDone", 1, _coerce_task_reason_field)
register_upcaster("AttemptClosed", 1, _coerce_close_reason_field)
register_upcaster("AttemptStarted", 1, _rename_simulated_provenance)
register_upcaster("ResultRecorded", 1, _rename_simulated_provenance)
register_upcaster("StudyFinalized", 1, _study_finalized_v1_to_v2)
