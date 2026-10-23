"""In-memory projection of M0 state-core events (extracted from machine.py)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from coscientist.core.domain import Study, Task, TaskOutcome, TaskState
from coscientist.core.events import EventEnvelope
from coscientist.core.graph import GraphEdge, GraphNode, GraphStore
from coscientist.core.reasons import TaskReason


class LocalProjectMixin:
    """Fold journal events into StudyRuntime local maps (no HITL/budget)."""

    study: Study | None
    tasks: dict[str, Task]
    attempts: dict[str, dict[str, Any]]
    results: dict[str, dict[str, Any]]
    plans: dict[str, dict[str, Any]]
    graph: GraphStore
    _retry_at: dict[str, datetime]
    _finalized: bool
    _study_cancelled: bool
    _budget_exceeded: bool

    def _project_local(self, ev: EventEnvelope) -> None:
        p = ev.payload
        if ev.type == "StudyCreated":
            self.study = Study.model_validate(p)
        elif ev.type == "PlanAccepted":
            self.plans[ev.entity_key] = dict(p)
        elif ev.type == "StudyCancelled":
            self._study_cancelled = True
        elif ev.type == "StudyFinalized":
            self._finalized = True
            self._fold_finalized_graph(ev.study_id, p, created_at=ev.ts)
        elif ev.type in ("TaskCreated", "TaskInherited"):
            self.tasks[p["task_id"]] = Task.model_validate(p)
        elif ev.type == "AttemptStarted":
            self.attempts[p["attempt_id"]] = dict(p)
            t = self.tasks[p["task_id"]]
            attempts = list(t.attempts)
            if p["attempt_id"] not in attempts:
                attempts.append(p["attempt_id"])
            self.tasks[p["task_id"]] = t.model_copy(
                update={"state": TaskState.RUNNING, "attempts": attempts, "reason": None}
            )
            self._retry_at.pop(p["task_id"], None)
        elif ev.type == "AttemptClosed":
            att = self.attempts.get(p["attempt_id"], {})
            att.update(p)
            att["closed"] = True
            self.attempts[p["attempt_id"]] = att
            tid = p.get("task_id") or att.get("task_id")
            if tid and p.get("retry_at"):
                t = self.tasks[tid]
                if t.state != TaskState.DONE:
                    retry_at = p["retry_at"]
                    if isinstance(retry_at, str):
                        retry_at = datetime.fromisoformat(retry_at)
                    self._retry_at[tid] = retry_at
                    self.tasks[tid] = t.model_copy(
                        update={
                            "state": TaskState.PENDING,
                            "reason": TaskReason.RETRY_SCHEDULED,
                        }
                    )
        elif ev.type == "RouteReturned":
            aid = p.get("attempt_id") or ev.entity_key
            att = self.attempts.get(aid, {})
            att = {
                **att,
                "route_returned": True,
                "agent_output_digest": p.get("agent_output_digest"),
            }
            self.attempts[aid] = att
        elif ev.type == "ResultRecorded":
            self.results[p["result_id"]] = dict(p)
            self.results[p["attempt_id"]] = dict(p)
        elif ev.type == "TaskDone":
            t = self.tasks[p["task_id"]]
            reason = p.get("reason")
            self.tasks[p["task_id"]] = t.model_copy(
                update={
                    "state": TaskState.DONE,
                    "outcome": TaskOutcome(p["outcome"]),
                    "reason": reason,
                }
            )
            self._retry_at.pop(p["task_id"], None)
            if reason == TaskReason.BUDGET.value or reason == TaskReason.BUDGET:
                self._budget_exceeded = True
        elif ev.type == "GraphNodeUpserted":
            self.graph.upsert_node(GraphNode.model_validate(p))

    def _fold_finalized_graph(
        self, study_id: str, payload: dict[str, Any], *, created_at: datetime
    ) -> None:
        from coscientist.core.finalize_nodes import graph_rows_from_finalized

        nodes, edges = graph_rows_from_finalized(
            study_id, payload, created_at=created_at
        )
        for row in nodes.values():
            self.graph.upsert_node(GraphNode.model_validate(row))
        for row in edges.values():
            try:
                self.graph.add_edge(GraphEdge.model_validate(row))
            except (KeyError, ValueError):
                # study/question node may be missing in sparse journals
                pass
