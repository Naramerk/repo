"""Fold events into projection tables (DESIGN §3.5a)."""

from __future__ import annotations

from typing import Any

from coscientist.core.events import EventEnvelope
from coscientist.core.reasons import TaskReason

# table -> row_key -> data
ProjectionMap = dict[str, dict[str, dict[str, Any]]]


def _put(proj: ProjectionMap, table: str, key: str, data: dict[str, Any]) -> None:
    proj.setdefault(table, {})[key] = data


def _get(proj: ProjectionMap, table: str, key: str) -> dict[str, Any] | None:
    return proj.get(table, {}).get(key)


def apply_to_map(proj: ProjectionMap, event: EventEnvelope) -> None:
    p = event.payload
    t = event.type
    if t == "StudyCreated":
        _put(proj, "studies", event.study_id, dict(p))
    elif t == "TaskCreated" or t == "TaskInherited":
        _put(proj, "tasks", event.entity_key, dict(p))
    elif t == "PlanAccepted":
        _put(proj, "plans", event.entity_key, dict(p))
    elif t == "StudyCancelled":
        study = _get(proj, "studies", event.study_id) or {}
        _put(proj, "studies", event.study_id, {**study, "cancelled": True, **p})
    elif t == "StudyFinalized":
        study = _get(proj, "studies", event.study_id) or {}
        _put(
            proj,
            "studies",
            event.study_id,
            {**study, "finalized": True, "finalized_payload": p},
        )
        _put(
            proj,
            "meta",
            event.study_id,
            {"study_id": event.study_id, "finalized": True},
        )
    elif t == "AttemptStarted":
        _put(proj, "attempts", event.entity_key, dict(p))
        task = _get(proj, "tasks", p["task_id"])
        if task is not None:
            attempts = list(task.get("attempts", []))
            if event.entity_key not in attempts:
                attempts.append(event.entity_key)
            task = {
                **task,
                "state": "RUNNING",
                "attempts": attempts,
            }
            _put(proj, "tasks", p["task_id"], task)
    elif t == "AttemptClosed":
        att = _get(proj, "attempts", event.entity_key) or {}
        att = {**att, **p, "closed": True}
        _put(proj, "attempts", event.entity_key, att)
        # P3: retryable close → task PENDING with retry_at (no RUNNING without attempt)
        tid = p.get("task_id") or att.get("task_id")
        if tid and p.get("retry_at"):
            task = _get(proj, "tasks", tid)
            if task is not None and task.get("state") != "DONE":
                _put(
                    proj,
                    "tasks",
                    tid,
                    {
                        **task,
                        "state": "PENDING",
                        "reason": TaskReason.RETRY_SCHEDULED.value,
                        "retry_at": p["retry_at"],
                    },
                )
    elif t == "RouteReturned":
        att = _get(proj, "attempts", event.entity_key) or {}
        att = {**att, "route_returned": True, "agent_output_digest": p.get("agent_output_digest")}
        _put(proj, "attempts", event.entity_key, att)
    elif t == "ResultRecorded":
        _put(proj, "results", event.entity_key, dict(p))
    elif t == "TaskDone":
        task = _get(proj, "tasks", event.entity_key) or {}
        task = {
            **task,
            "state": "DONE",
            "outcome": p.get("outcome"),
            "reason": p.get("reason"),
        }
        _put(proj, "tasks", event.entity_key, task)
    elif t == "ArtifactRegistered":
        _put(proj, "artifacts", event.entity_key, dict(p))
    elif t == "HitlOpened":
        _put(proj, "hitl_requests", event.entity_key, {**p, "open": True})
    elif t in ("HitlDecided", "HitlTimedOut"):
        hitl = _get(proj, "hitl_requests", event.entity_key) or {}
        keep_open = bool(p.get("keep_open") or p.get("effect") == "WAIT")
        hitl = {**hitl, **p, "open": keep_open}
        _put(proj, "hitl_requests", event.entity_key, hitl)
    elif t == "HitlDeadlineExtended":
        hitl = _get(proj, "hitl_requests", event.entity_key) or {}
        hitl = {**hitl, "deadline": p.get("deadline", hitl.get("deadline"))}
        _put(proj, "hitl_requests", event.entity_key, hitl)
    elif t == "GraphNodeUpserted":
        _put(proj, "graph_nodes", event.entity_key, dict(p))
    elif t == "GraphEdgeAdded":
        _put(proj, "graph_edges", event.entity_key, dict(p))
    elif t == "RequirementUpserted":
        _put(proj, "requirements", event.entity_key, dict(p))
    elif t == "ReviewScheduled":
        # tracked on requirements / results; keep a lightweight row
        _put(proj, "requirements", f"review:{event.entity_key}", dict(p))


def fold_events(events: list[EventEnvelope]) -> ProjectionMap:
    proj: ProjectionMap = {}
    for ev in events:
        apply_to_map(proj, ev)
    return proj


def apply_event(store: Any, event: EventEnvelope) -> None:
    """Apply one event into the store's projection table (in-txn write connection).

    Uses ``_get_projection_write`` / ``_upsert_projection_write`` when present so
    projection updates stay on the fenced writer connection inside append.
    """
    get_rows = getattr(store, "_get_projection_write", None)
    put_row = getattr(store, "_upsert_projection_write", None)
    if get_rows is None or put_row is None:
        raise TypeError(
            "apply_event requires a write-capable store "
            "(_get_projection_write / _upsert_projection_write)"
        )
    scratch: ProjectionMap = {}
    for table in (
        "studies",
        "plans",
        "tasks",
        "attempts",
        "results",
        "artifacts",
        "requirements",
        "hitl_requests",
        "graph_nodes",
        "graph_edges",
        "meta",
    ):
        for row in get_rows(table):
            key = _row_key(table, row)
            if key:
                scratch.setdefault(table, {})[key] = row
    apply_to_map(scratch, event)
    affected = _affected_tables(event.type)
    for table in affected:
        for key, data in scratch.get(table, {}).items():
            put_row(table, key, data)


def _row_key(table: str, row: dict[str, Any]) -> str | None:
    mapping = {
        "studies": "study_id",
        "plans": "plan_id",
        "tasks": "task_id",
        "attempts": "attempt_id",
        "results": "result_id",
        "artifacts": "artifact_id",
        "requirements": "id",
        "hitl_requests": "request_id",
        "graph_nodes": "node_id",
        "graph_edges": "edge_id",
        "meta": "study_id",
    }
    field = mapping.get(table)
    if table == "meta":
        return str(row["study_id"]) if row.get("study_id") else row.get("row_key")
    return str(row[field]) if field and field in row else row.get("row_key")


def _affected_tables(event_type: str) -> tuple[str, ...]:
    return {
        "StudyCreated": ("studies",),
        "TaskCreated": ("tasks",),
        "TaskInherited": ("tasks",),
        "PlanAccepted": ("plans",),
        "StudyCancelled": ("studies",),
        "StudyFinalized": ("studies", "meta"),
        "AttemptStarted": ("attempts", "tasks"),
        "AttemptClosed": ("attempts", "tasks"),
        "RouteReturned": ("attempts",),
        "ResultRecorded": ("results",),
        "TaskDone": ("tasks",),
        "ArtifactRegistered": ("artifacts",),
        "HitlOpened": ("hitl_requests",),
        "HitlDecided": ("hitl_requests",),
        "HitlTimedOut": ("hitl_requests",),
        "HitlDeadlineExtended": ("hitl_requests",),
        "GraphNodeUpserted": ("graph_nodes",),
        "GraphEdgeAdded": ("graph_edges",),
        "RequirementUpserted": ("requirements",),
        "ReviewScheduled": ("requirements",),
    }.get(event_type, ())
