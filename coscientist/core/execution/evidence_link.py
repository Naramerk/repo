"""Tool artifacts → bounded, source-addressed Evidence graph nodes.

The artifact-level node states projection coverage. Each displayed scalar has
its own EvidenceRef (RFC6901 pointer + actual CAS digest), so a report display
limit cannot silently remove later facts from a monolithic statement. Only
tool-provenance bytes are projected; model echoes never become observations.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from coscientist.core.claims.graph import GraphEdge, GraphNode, KindRegistry, registry_from_schema
from coscientist.core.claims.graph_writer import GraphWriterError, graph_edge_added, graph_node_upserted
from coscientist.core.events.digest import sha256_hex
from coscientist.core.events.events import EventEnvelope
from coscientist.core.events.ids import experiment_node_id, question_node_id
from coscientist.core.execution.artifacts import LocalArtifactStore, _infer_artifact_role
from coscientist.core.execution.domain import Artifact, ArtifactHandle, CheckResult, ReqStatus, Task
from coscientist.core.execution.fact_projection import DEFAULT_LIMITS, project_facts, reference_projection

log = logging.getLogger(__name__)


def artifact_node_id(study_id: str, artifact_id: str) -> str:
    if not study_id or not artifact_id:
        raise GraphWriterError("study_id and artifact_id must be non-empty")
    return f"{study_id}:artifact:{artifact_id}"


def evidence_node_id(study_id: str, artifact_id: str) -> str:
    if not study_id or not artifact_id:
        raise GraphWriterError("study_id and artifact_id must be non-empty")
    return f"{study_id}:evidence:{artifact_id}"


def _artifact_attrs(meta: Artifact) -> dict[str, object] | None:
    role = meta.role
    if role is None:
        role = _infer_artifact_role(meta.media_type)
    if role is None:
        return None
    attrs: dict[str, object] = {"artifact_id": meta.artifact_id, "role": role}
    if meta.media_type:
        attrs["media_type"] = meta.media_type
    if meta.sha256:
        attrs["sha256"] = meta.sha256
    if meta.name:
        attrs["title"] = meta.name
    return attrs


def _required_pointers(checks: Sequence[CheckResult], artifact_id: str) -> tuple[str, ...]:
    # Failed prediction checks contain legitimate contrary observations too.
    # Receipt values/digests are NEVER copied: selectors are re-resolved in CAS.
    return tuple(
        dict.fromkeys(
            ref.json_pointer
            for check in checks
            if check.required and check.status in (ReqStatus.MET, ReqStatus.FAILED)
            for ref in check.evidence
            if ref.artifact_id == artifact_id and ref.json_pointer is not None
        )
    )


def _evidence_node(
    *,
    node_id: str,
    study_id: str,
    meta: Artifact,
    pointer: str,
    statement: str,
    now: datetime,
) -> GraphNode:
    ref_id = node_id
    if node_id == evidence_node_id(study_id, meta.artifact_id):
        ref_id = meta.artifact_id
    return GraphNode(
        node_id=node_id,
        kind="evidence",
        study_id=study_id,
        ref_id=ref_id,
        attrs={
            "kind": "computational",
            "statement": statement,
            "ref": {"artifact_id": meta.artifact_id, "json_pointer": pointer, "digest": meta.sha256},
            "artifact_uri": str(meta.uri),
        },
        created_at=now,
    )


def _edge_event(
    kind: str,
    source: GraphNode,
    target: GraphNode,
    now: datetime,
    registry: KindRegistry,
) -> EventEnvelope:
    return graph_edge_added(
        edge=GraphEdge(
            edge_id=f"{kind}:{source.node_id}:{target.node_id}",
            kind=kind,
            study_id=source.study_id,
            src_id=source.node_id,
            dst_id=target.node_id,
            created_at=now,
        ),
        ts=now,
        src=source,
        dst=target,
        registry=registry,
        idem_extra=("evidence_link",),
    )


def evidence_link_events(
    *,
    study_id: str,
    task: Task | None,
    attempt_id: str,
    outputs: Mapping[str, ArtifactHandle],
    artifacts: LocalArtifactStore,
    now: datetime,
    graph: Any = None,
    registry: KindRegistry | None = None,
    check_results: Sequence[CheckResult] = (),
) -> list[EventEnvelope]:
    """Project observations into the same append batch as ResultRecorded.

    Parse refusals remain reference-only. Missing graph nodes only suppress
    their edges with a logged reason. CAS digest failures propagate; corrupted
    bytes must never yield observed facts or be silently treated as valid.
    """
    reg = registry
    if reg is None:
        reg = registry_from_schema()
    events: list[EventEnvelope] = []
    experiment = (
        graph.get_node(experiment_node_id(study_id, task.spec.task_key))
        if graph is not None and task is not None
        else None
    )
    questions: list[GraphNode] = []
    if task is not None:
        for qid in dict.fromkeys(task.spec.addresses):
            question = graph.get_node(question_node_id(study_id, qid)) if graph is not None else None
            if question is None:
                log.warning("evidence-link: question edge skipped; absent question=%s attempt=%s", qid, attempt_id)
            else:
                questions.append(question)
    for name, handle in outputs.items():
        meta = artifacts.get_meta(handle.artifact_id)
        if meta is None:
            log.warning("evidence-link: unregistered artifact skipped; output=%r attempt=%s", name, attempt_id)
            continue
        if meta.provenance != "tool":
            continue
        attrs = _artifact_attrs(meta)
        if attrs is None:
            log.warning("evidence-link: role unresolved; artifact=%r attempt=%s", meta.artifact_id, attempt_id)
            continue
        required = _required_pointers(check_results, meta.artifact_id)
        if meta.size is not None and meta.size > DEFAULT_LIMITS.max_bytes:
            projection = reference_projection("byte_budget", required)
        else:
            # read() verifies the actual CAS digest before any extraction.
            projection = project_facts(artifacts.read(meta.artifact_id), required_pointers=required)
        artifact = GraphNode(
            node_id=artifact_node_id(study_id, meta.artifact_id),
            kind="artifact",
            study_id=study_id,
            ref_id=meta.artifact_id,
            attrs=attrs,
            created_at=now,
        )
        root_id = evidence_node_id(study_id, meta.artifact_id)
        observations = [
            _evidence_node(
                node_id=root_id,
                study_id=study_id,
                meta=meta,
                pointer="",
                statement=f"{projection.summary}; материал={meta.size} байт",
                now=now,
            )
        ]
        observations.extend(
            _evidence_node(
                node_id=f"{root_id}:fact:{sha256_hex(fact.pointer.encode('utf-8'))}",
                study_id=study_id,
                meta=meta,
                pointer=fact.pointer,
                statement=fact.statement,
                now=now,
            )
            for fact in projection.facts
        )
        try:
            events.append(graph_node_upserted(node=artifact, ts=now, registry=reg, idem_extra=("evidence_link",)))
            if experiment is not None:
                events.append(_edge_event("produced", experiment, artifact, now, reg))
            for observation in observations:
                events.append(
                    graph_node_upserted(node=observation, ts=now, registry=reg, idem_extra=("evidence_link",))
                )
                events.append(_edge_event("derived_from", observation, artifact, now, reg))
                for question in questions:
                    events.append(_edge_event("relates_to", observation, question, now, reg))
        except GraphWriterError as exc:
            log.warning(
                "evidence-link: graph writer refused artifact=%r attempt=%s: %s", meta.artifact_id, attempt_id, exc
            )
    return events
