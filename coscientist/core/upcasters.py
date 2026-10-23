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


register_upcaster("TaskCreated", 1, _task_created_v1_to_v2)
register_upcaster("TaskDone", 1, _coerce_task_reason_field)
register_upcaster("AttemptClosed", 1, _coerce_close_reason_field)
register_upcaster("AttemptStarted", 1, _rename_simulated_provenance)
register_upcaster("ResultRecorded", 1, _rename_simulated_provenance)
