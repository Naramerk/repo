"""Graph + skip events for PlanExpanded (M1 PR9)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from coscientist.core.claims.graph import GraphEdge, GraphNode
from coscientist.core.claims.reasons import CloseReason, TaskReason
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.ids import question_node_id, task_id
from coscientist.core.execution.domain import (
    Need,
    QuestionSpec,
    QuestionSupersede,
    TaskOutcome,
    TaskState,
)
from coscientist.core.plan.supersede import plan_node_id

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime

DelegatedBy = Literal["human", "timeout"] | None


def plan_graph_events(
    runtime: StudyRuntime,
    *,
    sid: str,
    pid: str,
    prev_pid: str,
    new_rev: int,
    prev_rev: int,
    origin: str,
    delegated_by: DelegatedBy,
    now: Any,
    hypotheses: Need,
    literature: Need,
) -> list[EventEnvelope]:
    """Emit plan GraphNodeUpserted (+ supersedes).

    ``hypotheses`` / ``literature`` are required from the caller (already resolved
    via ``recorded_plan_needs``) — never invent Need stubs here.
    """
    events: list[EventEnvelope] = []
    pnid = plan_node_id(sid, pid)
    # ``origin`` is PlanExpanded payload only — not a plan KindSpec field.
    _ = origin
    needs = {
        "hypotheses": hypotheses.model_dump(mode="json"),
        "literature": literature.model_dump(mode="json"),
    }
    attrs: dict[str, object] = {"revision": new_rev, **needs}
    if delegated_by:
        attrs["delegated_by"] = delegated_by
        attrs["approved_by"] = "critic"
    events.append(
        EventEnvelope(
            study_id=sid,
            type="GraphNodeUpserted",
            entity_key=pnid,
            idem_key=make_idem("GraphNodeUpserted", pnid),
            payload=GraphNode(
                node_id=pnid,
                kind="plan",
                study_id=sid,
                ref_id=pid,
                attrs=attrs,
                created_at=now,
            ).model_dump(mode="json"),
            ts=now,
            schema_version=1,
        )
    )
    if not prev_pid:
        return events
    prev_nid = plan_node_id(sid, prev_pid)
    if runtime.graph.get_node(prev_nid) is None:
        events.append(
            EventEnvelope(
                study_id=sid,
                type="GraphNodeUpserted",
                entity_key=prev_nid,
                idem_key=make_idem("GraphNodeUpserted", prev_nid, "backfill"),
                payload=GraphNode(
                    node_id=prev_nid,
                    kind="plan",
                    study_id=sid,
                    ref_id=prev_pid,
                    attrs={"revision": prev_rev, **needs},
                    created_at=now,
                ).model_dump(mode="json"),
                ts=now,
                schema_version=1,
            )
        )
    eid = f"supersedes:{pnid}:{prev_nid}"
    events.append(
        EventEnvelope(
            study_id=sid,
            type="GraphEdgeAdded",
            entity_key=eid,
            idem_key=make_idem("GraphEdgeAdded", eid),
            payload=GraphEdge(
                edge_id=eid,
                kind="supersedes",
                study_id=sid,
                src_id=pnid,
                dst_id=prev_nid,
                attrs={},
                created_at=now,
            ).model_dump(mode="json"),
            ts=now,
            schema_version=1,
        )
    )
    return events


def _decomposes_parent_map(runtime: StudyRuntime, sid: str) -> dict[str, str | None]:
    """Existing decomposes edges as question_id → parent_id (plus known roots)."""
    prefix = f"{sid}:question:"
    parent_of: dict[str, str | None] = {}

    def _qid(node_id: str, node: Any) -> str:
        ref = getattr(node, "ref_id", None) if node is not None else None
        if ref:
            return str(ref)
        if node_id.startswith(prefix):
            return node_id[len(prefix) :]
        return node_id

    for e in runtime.graph.all_edges():
        if e.kind != "decomposes":
            continue
        child = _qid(e.src_id, runtime.graph.get_node(e.src_id))
        parent = _qid(e.dst_id, runtime.graph.get_node(e.dst_id))
        parent_of[child] = parent
        parent_of.setdefault(parent, None)
    for n in runtime.graph.nodes_by_kind("question"):
        qid = str(getattr(n, "ref_id", None) or "")
        if qid:
            parent_of.setdefault(qid, None)
    return parent_of


def question_graph_events(
    runtime: StudyRuntime,
    *,
    sid: str,
    pid: str,
    added_questions: list[QuestionSpec],
    superseded_questions: list[QuestionSupersede],
    known_texts: dict[str, str],
    now: Any,
) -> list[EventEnvelope]:
    events: list[EventEnvelope] = []
    from coscientist.core.plan.decomposes import assert_decomposes_acyclic

    # Full tree (existing edges + edges in this batch) — not just the new self-pair.
    parent_of = _decomposes_parent_map(runtime, sid)
    for q in added_questions:
        qnid = question_node_id(sid, q.id)
        if runtime.graph.get_node(qnid) is not None:
            continue
        events.append(
            EventEnvelope(
                study_id=sid,
                type="GraphNodeUpserted",
                entity_key=qnid,
                idem_key=make_idem("GraphNodeUpserted", qnid, pid),
                payload=GraphNode(
                    node_id=qnid,
                    kind="question",
                    study_id=sid,
                    ref_id=q.id,
                    attrs={"formulation": q.text},
                    created_at=now,
                ).model_dump(mode="json"),
                ts=now,
                schema_version=1,
            )
        )
        parent = getattr(q, "parent_id", None)
        if parent:
            # Acyclicity on every decomposes write (DESIGN_M1 §10).
            q_origin = getattr(q, "origin", None)
            edge_origin = (
                q_origin if q_origin in ("explicit", "implicit", "emergent") else "emergent"
            )
            parent_of[q.id] = str(parent)
            parent_of.setdefault(str(parent), None)
            assert_decomposes_acyclic(parent_of)
            pn = question_node_id(sid, str(parent))
            eid = f"decomposes:{qnid}:{pn}"
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="GraphEdgeAdded",
                    entity_key=eid,
                    idem_key=make_idem("GraphEdgeAdded", eid),
                    payload=GraphEdge(
                        edge_id=eid,
                        kind="decomposes",
                        study_id=sid,
                        src_id=qnid,
                        dst_id=pn,
                        attrs={"origin": edge_origin},
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=1,
                )
            )
    added_ids = {q.id for q in added_questions}
    for s in superseded_questions:
        new_nid = question_node_id(sid, s.new_id)
        old_nid = question_node_id(sid, s.old_id)
        if runtime.graph.get_node(old_nid) is None:
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=old_nid,
                    idem_key=make_idem("GraphNodeUpserted", old_nid, "supersede_old"),
                    payload=GraphNode(
                        node_id=old_nid,
                        kind="question",
                        study_id=sid,
                        ref_id=s.old_id,
                        attrs={"formulation": known_texts.get(s.old_id, "")},
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=1,
                )
            )
        if runtime.graph.get_node(new_nid) is None and s.new_id not in added_ids:
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=new_nid,
                    idem_key=make_idem("GraphNodeUpserted", new_nid, "supersede_new"),
                    payload=GraphNode(
                        node_id=new_nid,
                        kind="question",
                        study_id=sid,
                        ref_id=s.new_id,
                        attrs={"formulation": known_texts.get(s.new_id, "")},
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=1,
                )
            )
        eid = f"supersedes:{new_nid}:{old_nid}"
        events.append(
            EventEnvelope(
                study_id=sid,
                type="GraphEdgeAdded",
                entity_key=eid,
                idem_key=make_idem("GraphEdgeAdded", eid),
                payload=GraphEdge(
                    edge_id=eid,
                    kind="supersedes",
                    study_id=sid,
                    src_id=new_nid,
                    dst_id=old_nid,
                    attrs={},
                    created_at=now,
                ).model_dump(mode="json"),
                ts=now,
                schema_version=1,
            )
        )
    return events


def skip_superseded_task_events(
    runtime: StudyRuntime,
    *,
    sid: str,
    superseded_tasks: list[str],
    now: Any,
) -> list[EventEnvelope]:
    events: list[EventEnvelope] = []
    for tkey in superseded_tasks:
        tid = task_id(sid, tkey)
        task = runtime.tasks.get(tid)
        if task is None or task.state == TaskState.DONE:
            continue
        for aid in task.attempts:
            if not runtime.attempts.get(aid, {}).get("closed"):
                events.append(
                    EventEnvelope(
                        study_id=sid,
                        type="AttemptClosed",
                        entity_key=aid,
                        idem_key=make_idem("AttemptClosed", aid, CloseReason.CANCELLED.value),
                        payload={
                            "attempt_id": aid,
                            "closed": True,
                            "close_reason": CloseReason.CANCELLED.value,
                            "task_id": tid,
                        },
                        ts=now,
                        schema_version=2,
                    )
                )
        events.append(
            EventEnvelope(
                study_id=sid,
                type="TaskDone",
                entity_key=tid,
                idem_key=make_idem("TaskDone", tid, "superseded"),
                payload={
                    "task_id": tid,
                    "outcome": TaskOutcome.SKIPPED.value,
                    "reason": TaskReason.SUPERSEDED.value,
                },
                ts=now,
                schema_version=2,
            )
        )
    return events
