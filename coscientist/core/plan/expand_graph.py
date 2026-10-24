"""Graph + skip events for PlanExpanded (M1 PR9)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from coscientist.core.claims.graph import GraphEdge, GraphNode
from coscientist.core.claims.reasons import CloseReason, TaskReason
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.ids import question_node_id, task_id
from coscientist.core.events.schema_versions import (
    ATTEMPT_CLOSED_SCHEMA_VERSION,
    GRAPH_EDGE_ADDED_SCHEMA_VERSION,
    GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
    TASK_DONE_SCHEMA_VERSION,
)
from coscientist.core.execution.domain import QuestionSpec, QuestionSupersede, TaskOutcome, TaskState
from coscientist.core.plan.supersede import plan_node_id

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime

DelegatedBy = Literal["human", "timeout"] | None

def _require_known_text(known_texts: dict[str, str], qid: str) -> str:
    if qid not in known_texts:
        raise JournalFormatError(f"known_texts missing formulation for {qid!r}")
    text = known_texts[qid]
    if not isinstance(text, str):
        raise JournalFormatError(
            f"known_texts[{qid!r}] must be str, got {type(text).__name__}"
        )
    return text

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
) -> list[UnstampedEvent]:
    events = []
    pnid = plan_node_id(sid, pid)
    attrs: dict[str, Any] = {"revision": new_rev, "origin": origin}
    if delegated_by:
        attrs["delegated_by"] = delegated_by
        attrs["approved_by"] = "critic"
    events.append(
        UnstampedEvent(
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
            schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
                )
    )
    if not prev_pid:
        return events
    prev_nid = plan_node_id(sid, prev_pid)
    if runtime.graph.get_node(prev_nid) is None:
        events.append(
            UnstampedEvent(
                study_id=sid,
                type="GraphNodeUpserted",
                entity_key=prev_nid,
                idem_key=make_idem("GraphNodeUpserted", prev_nid, "backfill"),
                payload=GraphNode(
                    node_id=prev_nid,
                    kind="plan",
                    study_id=sid,
                    ref_id=prev_pid,
                    attrs={"revision": prev_rev},
                    created_at=now,
                ).model_dump(mode="json"),
                ts=now,
                schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
                )
        )
    eid = f"supersedes:{pnid}:{prev_nid}"
    events.append(
        UnstampedEvent(
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
            schema_version=GRAPH_EDGE_ADDED_SCHEMA_VERSION,
                )
    )
    return events

def _decomposes_parent_map(runtime: StudyRuntime) -> dict[str, str | None]:
    """Existing decomposes edges as question_id → parent_id (plus known roots)."""
    parent_of: dict[str, str | None] = {}  # audit-ok[field_default]: structural empty = collecting decomposes parents

    def _qid(node_id: str, node: Any) -> str:
        if node is None:
            raise ValueError(f"decomposes node missing for {node_id!r}")
        ref = node.ref_id
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"question node {node_id!r} missing required ref_id str")
        return ref

    def _ensure_root(qid: str) -> None:
        if qid not in parent_of:
            parent_of[qid] = None

    for e in runtime.graph.all_edges():
        if e.kind != "decomposes":
            continue
        child = _qid(e.src_id, runtime.graph.get_node(e.src_id))
        parent = _qid(e.dst_id, runtime.graph.get_node(e.dst_id))
        parent_of[child] = parent
        _ensure_root(parent)
    for n in runtime.graph.nodes_by_kind("question"):
        qid = n.ref_id
        if not isinstance(qid, str) or not qid:
            raise ValueError(f"question node {n.node_id!r} missing required ref_id str")
        _ensure_root(qid)
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
) -> list[UnstampedEvent]:
    events = []
    from coscientist.core.plan.decomposes import assert_decomposes_acyclic

    # Full tree (existing edges + edges in this batch) — not just the new self-pair.
    parent_of = _decomposes_parent_map(runtime)
    for q in added_questions:
        qnid = question_node_id(sid, q.id)
        if runtime.graph.get_node(qnid) is not None:
            continue
        events.append(
            UnstampedEvent(
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
                schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
                )
        )
        parent = q.parent_id
        if parent:
            # Acyclicity on every decomposes write (DESIGN_M1 §10).
            q_origin = q.origin
            if q_origin is None:
                raise JournalFormatError(
                    f"question {q.id!r} with parent_id requires origin"
                )
            if isinstance(q_origin, str):
                edge_origin = q_origin
            else:
                edge_origin = q_origin.value
            if edge_origin not in ("explicit", "implicit", "emergent"):
                raise JournalFormatError(
                    f"question {q.id!r} invalid origin {edge_origin!r}"
                )
            if not isinstance(parent, str) or not parent:
                raise JournalFormatError(
                    f"question {q.id!r} parent_id must be nonempty str"
                )
            parent_of[q.id] = parent
            if parent not in parent_of:
                parent_of[parent] = None
            assert_decomposes_acyclic(parent_of)
            pn = question_node_id(sid, parent)
            eid = f"decomposes:{qnid}:{pn}"
            events.append(
                UnstampedEvent(
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
                    schema_version=GRAPH_EDGE_ADDED_SCHEMA_VERSION,
                )
            )
    added_ids = {q.id for q in added_questions}
    for s in superseded_questions:
        new_nid = question_node_id(sid, s.new_id)
        old_nid = question_node_id(sid, s.old_id)
        if runtime.graph.get_node(old_nid) is None:
            events.append(
                UnstampedEvent(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=old_nid,
                    idem_key=make_idem("GraphNodeUpserted", old_nid, "supersede_old"),
                    payload=GraphNode(
                        node_id=old_nid,
                        kind="question",
                        study_id=sid,
                        ref_id=s.old_id,
                        attrs={"formulation": _require_known_text(known_texts, s.old_id)},
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
                )
            )
        if runtime.graph.get_node(new_nid) is None and s.new_id not in added_ids:
            events.append(
                UnstampedEvent(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=new_nid,
                    idem_key=make_idem("GraphNodeUpserted", new_nid, "supersede_new"),
                    payload=GraphNode(
                        node_id=new_nid,
                        kind="question",
                        study_id=sid,
                        ref_id=s.new_id,
                        attrs={"formulation": _require_known_text(known_texts, s.new_id)},
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
                )
            )
        eid = f"supersedes:{new_nid}:{old_nid}"
        events.append(
            UnstampedEvent(
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
                schema_version=GRAPH_EDGE_ADDED_SCHEMA_VERSION,
                )
        )
    return events

def skip_superseded_task_events(
    runtime: StudyRuntime,
    *,
    sid: str,
    superseded_tasks: list[str],
    now: Any,
) -> list[UnstampedEvent]:
    events = []
    for tkey in superseded_tasks:
        tid = task_id(sid, tkey)
        task = runtime.tasks.get(tid)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if task is None or task.state == TaskState.DONE:
            continue
        for aid in task.attempts:
            attempt = runtime.attempts.get(aid)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if attempt is None:
                raise JournalFormatError(
                    f"attempt {aid!r} missing from runtime.attempts"
                )
            if "closed" not in attempt:
                raise JournalFormatError(
                    f"attempt {aid!r} missing required closed field"
                )
            closed = attempt["closed"]
            if not isinstance(closed, bool):
                raise JournalFormatError(
                    f"attempt {aid!r} closed must be bool, got {type(closed).__name__}"
                )
            if closed:
                continue
            events.append(
                UnstampedEvent(
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
                    schema_version=ATTEMPT_CLOSED_SCHEMA_VERSION,
                )
            )
        events.append(
            UnstampedEvent(
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
                schema_version=TASK_DONE_SCHEMA_VERSION,
)
        )
    return events
