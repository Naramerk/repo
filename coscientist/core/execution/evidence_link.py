"""C1 evidence-link: journaled tool_call artifacts → evidence graph nodes.

The report digest is built from ``graph_nodes`` / ``graph_edges`` projections
only, so tool_call JSON contents must become graph nodes or the report lane
honestly writes «не получено» for real content numbers. This module journals,
in the SAME append batch as ``ResultRecorded`` (Executor._commit_result via
``extra_events``), per recorded output artifact:

- ``GraphNodeUpserted(kind=artifact)`` — identity from the registered
  ``ArtifactRegistered`` meta (role/media_type/sha256/title);
- ``GraphNodeUpserted(kind=evidence)`` — computational observation whose
  ``statement`` carries mechanically extracted facts from the JOURNALED CAS
  bytes (never model claims), ``ref`` = EvidenceRef(artifact_id, "", sha256),
  ``artifact_uri`` = the CAS uri from meta;
- ``GraphEdgeAdded(derived_from)`` evidence → artifact (schema pair);
- ``GraphEdgeAdded(produced)`` experiment → artifact (only when the folded
  graph already has the experiment node — the experiment builder owns those
  nodes; never invented here);
- ``GraphEdgeAdded(relates_to)`` evidence → question (per ``spec.addresses``,
  only when the question node exists — PlanAccepted writes them).

Facts extraction is mechanical with typed caps; oversize/non-JSON/deep/
over-count artifacts contribute a reference-only evidence (uri + digest +
size), never a crash and never invented content.

Events go through the G7 writer (``graph_node_upserted`` /
``graph_edge_added``) so kinds/attrs/pairs validate against the schema and
idem keys make replay a no-op. Evidence is journaled ONLY for
``provenance="tool"`` artifacts — the journal stamp meaning «bytes are this
attempt's recorded tool_call result» (set by the trusted CLI journal
``record_executor_route`` / ``put_sandbox``; model-authored values echoes
carry ``producer="agent"`` and are excluded by that stamp, never by a
heuristic).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from coscientist.core.claims.graph import (
    GraphEdge,
    GraphNode,
    KindRegistry,
    registry_from_schema,
)
from coscientist.core.claims.graph_writer import (
    GraphWriterError,
    graph_edge_added,
    graph_node_upserted,
)
from coscientist.core.events.events import EventEnvelope
from coscientist.core.events.ids import experiment_node_id, question_node_id
from coscientist.core.execution.artifacts import (
    LocalArtifactStore,
    _infer_artifact_role,
)
from coscientist.core.execution.domain import ArtifactHandle, Task
from coscientist.core.finalize.minimal_report import MAX_BYTES as FACTS_MAX_BYTES

log = logging.getLogger(__name__)

__all__ = [
    "FACTS_MAX_BYTES",
    "artifact_node_id",
    "evidence_link_events",
    "evidence_node_id",
    "extract_facts",
]

# Parse-budget cap note: FACTS_MAX_BYTES reuses the existing minimal-report
# 10 KiB constant (same budget class — a bounded deterministic projection of
# journaled bytes), no invented number. Larger artifacts stay reference-only
# evidence and their bytes are never read (size pre-check below).

# Typed leaf caps for the mechanical facts walk (bounded depth/count — not
# heuristics): walk stops, evidence stays honest via the truncation suffix.
_MAX_DEPTH = 4
_MAX_LEAVES = 32
_MAX_LEAF_CHARS = 200

_FactsMode = Literal["json_facts", "reference_oversized", "reference_non_json"]


def _refuse_non_finite(raw: str) -> float:
    raise ValueError(f"non-finite JSON constant not allowed in facts: {raw}")


def _clip_leaf(text: str) -> str:
    if len(text) > _MAX_LEAF_CHARS:
        return text[:_MAX_LEAF_CHARS] + "…"
    return text


def _render_leaf(value: Any) -> str:
    return _clip_leaf(json.dumps(value, ensure_ascii=False))


def _walk_facts(
    node: Any,
    path: str,
    depth: int,
    out: list[str],
) -> bool:
    """Append ``path = value`` leaves in document order; True when truncated.

    Mechanical: dict/list traversal with typed caps; scalars render via
    ``json.dumps`` exactly as parsed (no rounding, no unit conversion).
    """
    if len(out) >= _MAX_LEAVES:
        return True
    if isinstance(node, dict):
        if depth >= _MAX_DEPTH:
            return True
        truncated = False
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if _walk_facts(value, child, depth + 1, out):
                truncated = True
                if len(out) >= _MAX_LEAVES:
                    break
        return truncated
    if isinstance(node, list):
        if depth >= _MAX_DEPTH:
            return True
        truncated = False
        for i, value in enumerate(node):
            child = f"{path}.{i}" if path else str(i)
            if _walk_facts(value, child, depth + 1, out):
                truncated = True
                if len(out) >= _MAX_LEAVES:
                    break
        return truncated
    out.append(f"{path} = {_render_leaf(node)}" if path else _render_leaf(node))
    return False


def extract_facts(raw: bytes) -> tuple[_FactsMode, str]:
    """Journaled artifact bytes → ``(mode, facts tail for the statement)``.

    ``json_facts`` — parsed key/value leaves (bounded); ``reference_oversized``
    — byte size over :data:`FACTS_MAX_BYTES` (callers pre-check ``meta.size``
    so oversized CAS blobs are never read); ``reference_non_json`` — JSON
    parse failed (incl. non-finite constants).
    """
    if len(raw) > FACTS_MAX_BYTES:
        return "reference_oversized", ""
    parse_failed = True
    parse_err = "JSONDecodeError"
    doc: object
    found_doc = False
    try:
        doc = json.loads(raw, parse_constant=_refuse_non_finite)
        parse_failed = False
        found_doc = True
    except (ValueError, UnicodeDecodeError) as e:
        parse_err = type(e).__name__  # audit-ok[except_enum_default]: error class is the log payload
    if parse_failed or not found_doc:
        log.info("evidence-link: JSON parse failed (%s); reference-only", parse_err)
        return "reference_non_json", ""
    leaves: list[str] = []
    truncated = _walk_facts(doc, "", 0, leaves)
    body = "; ".join(leaves[:_MAX_LEAVES])
    if truncated:
        body += "; разбор ограничен (показаны не все факты)"
    return "json_facts", body


def artifact_node_id(study_id: str, artifact_id: str) -> str:
    if not study_id or not artifact_id:
        raise GraphWriterError("study_id and artifact_id must be non-empty")
    return f"{study_id}:artifact:{artifact_id}"


def evidence_node_id(study_id: str, artifact_id: str) -> str:
    if not study_id or not artifact_id:
        raise GraphWriterError("study_id and artifact_id must be non-empty")
    return f"{study_id}:evidence:{artifact_id}"


def _artifact_attrs(meta: Any) -> dict[str, object] | None:
    """Registered meta → artifact node attrs (None when role unresolvable)."""
    role = meta.role
    if role is None:
        media = str(meta.media_type)
        if not media:
            media = ""
        role = _infer_artifact_role(media)
    attrs: dict[str, object] = {
        "artifact_id": str(meta.artifact_id),
        "role": str(role),
    }
    if meta.media_type:
        attrs["media_type"] = str(meta.media_type)
    if meta.sha256:
        attrs["sha256"] = str(meta.sha256)
    if meta.name:
        attrs["title"] = str(meta.name)
    return attrs


def _statement(mode: _FactsMode, facts: str, meta: Any) -> str:
    raw_tool = str(getattr(meta.produced_by, "tool", ""))
    tool = raw_tool
    if mode == "json_facts":
        head = f"Результат инструмента {tool}: " if tool else "Результат вызова инструмента: "
        return head + facts
    size = meta.size
    name = str(meta.name)
    if mode == "reference_oversized":
        reason = f"размер превышает лимит разбора {FACTS_MAX_BYTES} байт"
    else:
        reason = "JSON-разбор не удался"
    material = f"Материал попытки: {name} " if name else "Материал попытки "
    return (
        f"{material}({meta.media_type}), {size} байт; "
        f"содержимое не разобрано ({reason}); факт-ссылка сохранена"
    )


def evidence_link_events(
    *,
    study_id: str,
    task: Task | None,
    attempt_id: str,
    outputs: Mapping[str, ArtifactHandle],
    artifacts: LocalArtifactStore,
    now: datetime,
    graph: Any | None = None,
    registry: KindRegistry | None = None,
) -> list[EventEnvelope]:
    """Build G7 evidence-link events for the attempt's recorded tool artifacts.

    Never raises for per-artifact problems: a missing registration, an
    unresolvable role or a missing question node skips that piece with a
    logged reason (narrow skip, no silent catch-all) — recording itself must
    not fail because the additive evidence projection cannot resolve.
    """
    reg = registry
    if reg is None:
        reg = registry_from_schema()
    events: list[EventEnvelope] = []
    exp_node = (
        graph.get_node(experiment_node_id(study_id, task.spec.task_key))
        if graph is not None and task is not None
        else None
    )
    for name, handle in outputs.items():
        artifact_id = handle.artifact_id
        if not artifact_id:
            log.warning(
                "evidence-link: skipping output %r (no artifact_id) attempt=%s",
                name,
                attempt_id,
            )
            continue
        meta = artifacts.get_meta(artifact_id)
        if meta is None:
            log.warning(
                "evidence-link: skipping artifact %r (not registered) attempt=%s",
                artifact_id,
                attempt_id,
            )
            continue
        if meta.provenance != "tool":
            # Model-authored values echo — never cited as observed evidence.
            continue
        attrs = _artifact_attrs(meta)
        if attrs is None:
            log.warning(
                "evidence-link: skipping artifact %r (role unresolvable) attempt=%s",
                artifact_id,
                attempt_id,
            )
            continue
        ts = now
        art_nid = artifact_node_id(study_id, artifact_id)
        art_node = GraphNode(
            node_id=art_nid,
            kind="artifact",
            study_id=study_id,
            ref_id=artifact_id,
            attrs=attrs,
            created_at=ts,
        )
        # Size pre-check (journal meta): oversized CAS blobs are never read.
        mode: _FactsMode
        if meta.size is not None and meta.size > FACTS_MAX_BYTES:
            mode, facts = "reference_oversized", ""
        else:
            mode, facts = extract_facts(artifacts.read(artifact_id))
        ev_nid = evidence_node_id(study_id, artifact_id)
        ev_node = GraphNode(
            node_id=ev_nid,
            kind="evidence",
            study_id=study_id,
            ref_id=artifact_id,
            attrs={
                "kind": "computational",
                "statement": _statement(mode, facts, meta),
                "ref": {
                    "artifact_id": artifact_id,
                    "json_pointer": "",
                    "digest": str(meta.sha256),
                },
                "artifact_uri": str(meta.uri),
            },
            created_at=ts,
        )
        try:
            events.append(
                graph_node_upserted(node=art_node, ts=ts, registry=reg, idem_extra=("evidence_link",))
            )
            events.append(
                graph_node_upserted(node=ev_node, ts=ts, registry=reg, idem_extra=("evidence_link",))
            )
            events.append(
                graph_edge_added(
                    edge=_edge(
                        f"derived_from:{ev_nid}:{art_nid}",
                        "derived_from",
                        study_id,
                        ev_nid,
                        art_nid,
                        ts,
                    ),
                    ts=ts,
                    src=ev_node,
                    dst=art_node,
                    registry=reg,
                    idem_extra=("evidence_link",),
                )
            )
        except GraphWriterError as e:
            log.warning(
                "evidence-link: skipping artifact %r (writer refused: %s) attempt=%s",
                artifact_id,
                e,
                attempt_id,
            )
            continue  # audit-ok[except_silent_continue]: narrow per-artifact skip, reason logged
        if exp_node is not None:
            eid = f"produced:{exp_node.node_id}:{art_nid}"
            try:
                events.append(
                    graph_edge_added(
                        edge=_edge(eid, "produced", study_id, exp_node.node_id, art_nid, ts),
                        ts=ts,
                        src=exp_node,
                        dst=art_node,
                        registry=reg,
                        idem_extra=("evidence_link",),
                    )
                )
            except GraphWriterError as e:
                log.warning(
                    "evidence-link: produced edge skipped for %r (%s) attempt=%s",
                    artifact_id,
                    e,
                    attempt_id,
                )
        if task is not None:
            addresses: tuple[object, ...] = ()
            if task.spec.addresses:
                addresses = tuple(task.spec.addresses)
            for qid in dict.fromkeys(str(a) for a in addresses if a):
                q_nid = question_node_id(study_id, qid)
                q_node = graph.get_node(q_nid) if graph is not None else None
                if q_node is None:
                    log.warning(
                        "evidence-link: relates_to skipped for %r (question node %s absent)",
                        artifact_id,
                        q_nid,
                    )
                    continue
                eid = f"relates_to:{ev_nid}:{q_nid}"
                try:
                    events.append(
                        graph_edge_added(
                            edge=_edge(eid, "relates_to", study_id, ev_nid, q_nid, ts),
                            ts=ts,
                            src=ev_node,
                            dst=q_node,
                            registry=reg,
                            idem_extra=("evidence_link",),
                        )
                    )
                except GraphWriterError as e:
                    log.warning(
                        "evidence-link: relates_to edge skipped for %r (%s)",
                        artifact_id,
                        e,
                    )
    return events


def _edge(
    edge_id: str,
    kind: str,
    study_id: str,
    src_id: str,
    dst_id: str,
    ts: datetime,
) -> GraphEdge:
    return GraphEdge(
        edge_id=edge_id,
        kind=kind,
        study_id=study_id,
        src_id=src_id,
        dst_id=dst_id,
        created_at=ts,
    )
