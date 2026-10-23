"""In-memory projection of M0 state-core events (extracted from machine.py)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from coscientist.core.domain import Study, Task, TaskOutcome, TaskState
from coscientist.core.events import EventEnvelope
from coscientist.core.graph import GraphEdge, GraphNode, GraphStore
from coscientist.core.intake import IntakeRefuse
from coscientist.core.reasons import RefusalKind, TaskReason


class LocalProjectMixin:
    """Fold journal events into StudyRuntime local maps (no HITL)."""

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
    _intake_refusal: IntakeRefuse | None
    _study_spent_rub: Decimal
    verdict_snapshots: dict[str, dict[str, Any]]

    def _project_local(self, ev: EventEnvelope) -> None:
        p = ev.payload
        if ev.type == "StudyCreated":
            self.study = Study.model_validate(p)
        elif ev.type == "StudyRefused":
            self._intake_refusal = IntakeRefuse(
                kind=RefusalKind(p["kind"]),
                reason=str(p.get("reason") or ""),
                classifier_error=bool(p.get("classifier_error", False)),
            )
        elif ev.type == "BudgetDebited":
            self._study_spent_rub += Decimal(str(p.get("spent_rub") or 0))
        elif ev.type == "BudgetExceeded":
            if str(p.get("scope") or "study") == "study":
                self._budget_exceeded = True
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
            # TaskDone(reason=budget) is task-level only; study budget is
            # BudgetExceeded (folded above).
        elif ev.type == "GraphNodeUpserted":
            node = GraphNode.model_validate(p)
            # Never clobber richer existing question/hypothesis attrs with empty.
            existing = self.graph.get_node(node.node_id)
            if (
                existing is not None
                and existing.kind in ("question", "hypothesis")
                and node.kind == existing.kind
                and not (node.attrs or {})
                and (existing.attrs or {})
            ):
                pass  # keep existing
            else:
                self.graph.upsert_node(node)
            if node.kind == "claim":
                self._invalidate_verdict_snapshots(node)
        elif ev.type == "GraphEdgeAdded":
            # Fail loudly: missing endpoints / illegal pairs are journal bugs.
            edge = GraphEdge.model_validate(p)
            self.graph.add_edge(edge)
            if edge.kind == "about":
                self.verdict_snapshots.clear()
        elif ev.type == "VerdictSnapshotsRecorded":
            snaps = p.get("snapshots") or {}
            if isinstance(snaps, dict):
                self.verdict_snapshots.update(
                    {str(k): dict(v) for k, v in snaps.items() if isinstance(v, dict)}
                )

    def _invalidate_verdict_snapshots(self, node: GraphNode) -> None:
        """Drop journaled snapshots for a claim's target (DESIGN_M1 PR8)."""
        target = (node.attrs or {}).get("target")
        if target is not None and str(target) in self.verdict_snapshots:
            self.verdict_snapshots.pop(str(target), None)
        else:
            # Unknown target mapping — invalidate all (safe).
            self.verdict_snapshots.clear()

    def _fold_finalized_graph(
        self, study_id: str, payload: dict[str, Any], *, created_at: datetime
    ) -> None:
        from coscientist.core.finalize_nodes import graph_rows_from_finalized

        nodes, edges = graph_rows_from_finalized(
            study_id, payload, created_at=created_at
        )
        for row in nodes.values():
            incoming = GraphNode.model_validate(row)
            existing = self.graph.get_node(incoming.node_id)
            if (
                existing is not None
                and existing.kind in ("question", "hypothesis")
                and (existing.attrs or {})
                and not (incoming.attrs or {}).get("formulation")
            ):
                # Preserve formulation from accept_plan when finalize row is sparse.
                merged = incoming.model_copy(
                    update={"attrs": {**(existing.attrs or {}), **(incoming.attrs or {})}}
                )
                self.graph.upsert_node(merged)
            else:
                self.graph.upsert_node(incoming)
        for row in edges.values():
            try:
                self.graph.add_edge(GraphEdge.model_validate(row))
            except (KeyError, ValueError):
                # study/question node may be missing in sparse journals
                pass
        hv = payload.get("hypothesis_verdicts")
        if isinstance(hv, dict):
            self.verdict_snapshots.update(
                {str(k): dict(v) for k, v in hv.items() if isinstance(v, dict)}
            )
