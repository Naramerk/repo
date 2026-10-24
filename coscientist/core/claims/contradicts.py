"""``contradicts`` claim→claim edges (M1 PR15 / DESIGN_M1 §6.5).

One unordered pair per opposite-stance accepted claims about the same target.
Edge id = ``{study_id}:contradicts:{min}:{max}``; repeat → Idempotent.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from coscientist.core.claims.experiment_design import ARTIFACT_KIND_CONTRADICTION_MAP
from coscientist.core.claims.graph import GraphEdge, GraphStore
from coscientist.core.claims.stance import claim_stance
from coscientist.core.events.digest import sha256_hex
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.schema_versions import (
    GRAPH_EDGE_ADDED_SCHEMA_VERSION,
)

__all__ = [
    "contradicts_edge_id",
    "contradicts_events_for_new_claims",
    "contradiction_pairs",
    "render_contradiction_map",
]


def contradicts_edge_id(study_id: str, a: str, b: str) -> str:
    lo, hi = (a, b) if a <= b else (b, a)
    return f"{study_id}:contradicts:{lo}:{hi}"


def _stance_of(attrs: dict[str, Any]) -> str:
    try:
        status = attrs["criterion_status"]
    except KeyError as e:
        raise JournalFormatError("claim attrs missing 'criterion_status'") from e
    return claim_stance(status)


def _require_bool_attr(attrs: dict[str, Any], key: str) -> bool:
    try:
        raw = attrs[key]
    except KeyError as e:
        raise JournalFormatError(f"claim attrs missing {e.args[0]!r}") from e
    if not isinstance(raw, bool):
        raise JournalFormatError(
            f"claim attrs {key!r} must be bool, got {type(raw).__name__}"
        )
    return raw


def _accepted(attrs: dict[str, Any]) -> bool:
    return _require_bool_attr(attrs, "accepted") and _require_bool_attr(attrs, "frozen")


def _claim_target_from_attrs(attrs: dict[str, Any]) -> str | None:
    """Return attrs.target when a non-empty str; else None (no invent)."""
    if "target" not in attrs:
        return None
    raw = attrs["target"]
    if not isinstance(raw, str) or not raw:
        return None
    return raw


def contradiction_pairs(
    graph: GraphStore,
    *,
    study_id: str,
) -> list[tuple[str, str, str]]:
    """Return ``(claim_a, claim_b, target)`` for opposite-stance accepted pairs.

    ``claim_a < claim_b`` lexicographically.
    """
    claims = [n for n in graph.nodes_by_kind("claim") if n.study_id == study_id]
    by_target: dict[str, list[tuple[str, str]]] = {}  # audit-ok[field_default]: structural empty = no targets yet
    for n in claims:
        attrs = dict(n.attrs)
        if not _accepted(attrs):
            continue
        st = _stance_of(attrs)
        if st not in ("supports", "refutes"):
            continue
        # Target from about edge (preferred) or attrs.target.
        about = [e for e in graph.edges_from(n.node_id) if e.kind == "about"]
        if about:
            target = about[0].dst_id
        else:
            maybe = _claim_target_from_attrs(attrs)
            if maybe is None:
                continue
            target = maybe
        if target not in by_target:
            by_target[target] = []
        by_target[target].append((n.node_id, st))

    out: list[tuple[str, str, str]] = []  # audit-ok[field_default]: structural empty = no pairs yet
    seen: set[tuple[str, str]] = set()
    for target, rows in by_target.items():
        for i, (a, sa) in enumerate(rows):
            for b, sb in rows[i + 1 :]:
                if sa == sb:
                    continue
                if {sa, sb} != {"supports", "refutes"}:
                    continue
                lo, hi = (a, b) if a <= b else (b, a)
                key = (lo, hi)
                if key in seen:
                    continue
                seen.add(key)
                out.append((lo, hi, target))
    return out


def contradicts_events_for_new_claims(
    *,
    study_id: str,
    graph: GraphStore | None,
    now: datetime,
    new_claim_ids: set[str] | None = None,
    pending_claim_events: list[UnstampedEvent] | None = None,
) -> list[UnstampedEvent]:
    """Emit ``GraphEdgeAdded`` contradicts for pairs involving ``new_claim_ids``.

    When ``new_claim_ids`` is None, emit all missing pairs (finalize / repair).
    ``pending_claim_events`` are same-batch ``GraphNodeUpserted`` claim nodes not
    yet folded into ``graph`` (live == replay: edges journaled with claims).
    """
    if graph is None and not pending_claim_events:
        return []
    # Overlay pending claim nodes onto a shallow view for pair detection.
    from coscientist.core.claims.graph import GraphNode

    overlay: dict[str, GraphNode] = {}  # audit-ok[field_default]: structural empty = no pending overlay
    pending_about: dict[str, str] = {}  # audit-ok[field_default]: structural empty = no pending about edges
    if pending_claim_events is None:
        pending_iter: list[UnstampedEvent] = []  # audit-ok[field_default]: structural empty = no pending events
    else:
        pending_iter = pending_claim_events
    for ev in pending_iter:
        if ev.type == "GraphNodeUpserted":
            try:
                node = GraphNode.model_validate(ev.payload)
            except ValidationError as e:
                raise JournalFormatError(
                    f"GraphNodeUpserted payload invalid: {type(e).__name__}"
                ) from e
            if node.kind == "claim":
                overlay[node.node_id] = node
        elif ev.type == "GraphEdgeAdded":
            payload = ev.payload
            if not isinstance(payload, dict):
                raise JournalFormatError(
                    f"GraphEdgeAdded payload must be dict, got {type(payload).__name__}"
                )
            if "kind" not in payload:
                raise JournalFormatError("GraphEdgeAdded missing 'kind'")
            if payload["kind"] != "about":
                continue
            if "src_id" not in payload or "dst_id" not in payload:
                raise JournalFormatError("GraphEdgeAdded about missing src_id/dst_id")
            src = payload["src_id"]
            dst = payload["dst_id"]
            if not isinstance(src, str) or not src:
                raise JournalFormatError("GraphEdgeAdded about src_id must be non-empty str")
            if not isinstance(dst, str) or not dst:
                raise JournalFormatError("GraphEdgeAdded about dst_id must be non-empty str")
            pending_about[src] = dst

    def _attrs(cid: str) -> dict[str, Any] | None:
        if cid in overlay:
            return dict(overlay[cid].attrs)
        if graph is not None:
            n = graph.get_node(cid)
            if n is not None and n.kind == "claim":
                return dict(n.attrs)
        return None

    def _target(cid: str, attrs: dict[str, Any]) -> str | None:
        if cid in pending_about:
            return pending_about[cid]
        if graph is not None:
            about = [e for e in graph.edges_from(cid) if e.kind == "about"]
            if about:
                return about[0].dst_id
        return _claim_target_from_attrs(attrs)

    # Collect accepted opposite-stance claims by target.
    by_target: dict[str, list[tuple[str, str]]] = {}  # audit-ok[field_default]: structural empty = no targets yet
    claim_ids: set[str] = set(overlay)
    if graph is not None:
        claim_ids |= {n.node_id for n in graph.nodes_by_kind("claim") if n.study_id == study_id}
    for cid in claim_ids:
        attrs = _attrs(cid)
        if attrs is None or not _accepted(attrs):
            continue
        st = _stance_of(attrs)
        if st not in ("supports", "refutes"):
            continue
        tgt = _target(cid, attrs)
        if not tgt:
            continue
        if tgt not in by_target:
            by_target[tgt] = []
        by_target[tgt].append((cid, st))

    events: list[UnstampedEvent] = []  # audit-ok[field_default]: structural empty = no edges yet
    seen: set[tuple[str, str]] = set()
    for target, rows in by_target.items():
        for i, (a, sa) in enumerate(rows):
            for b, sb in rows[i + 1 :]:
                if {sa, sb} != {"supports", "refutes"}:
                    continue
                lo, hi = (a, b) if a <= b else (b, a)
                if (lo, hi) in seen:
                    continue
                seen.add((lo, hi))
                if new_claim_ids is not None and a not in new_claim_ids and b not in new_claim_ids:
                    continue
                eid = contradicts_edge_id(study_id, lo, hi)
                if graph is not None and any(e.edge_id == eid for e in graph.all_edges()):
                    continue
                edge = GraphEdge(
                    edge_id=eid,
                    kind="contradicts",
                    study_id=study_id,
                    src_id=lo,
                    dst_id=hi,
                    attrs={"target": target},
                    created_at=now,
                )
                events.append(
                    UnstampedEvent(
                        study_id=study_id,
                        type="GraphEdgeAdded",
                        entity_key=eid,
                        idem_key=make_idem("GraphEdgeAdded", eid),
                        payload=edge.model_dump(mode="json"),
                        ts=now,
                        schema_version=GRAPH_EDGE_ADDED_SCHEMA_VERSION,
                    )
                )
    return events


def render_contradiction_map(
    graph: GraphStore,
    *,
    study_id: str,
) -> tuple[bytes, str]:
    """Deterministic JSON map of contradicts edges → (bytes, sha256).

    Used when ``final_type=artifact`` contradiction_map is produced by code.
    """
    pairs = []
    for e in graph.all_edges():
        if e.kind != "contradicts" or e.study_id != study_id:
            continue
        if "target" not in e.attrs:
            raise JournalFormatError(f"contradicts edge {e.edge_id!r} missing attrs.target")
        pairs.append(
            {
                "a": e.src_id,
                "b": e.dst_id,
                "target": e.attrs["target"],
            }
        )
    pairs.sort(key=lambda r: (r["a"], r["b"]))
    payload = {
        "kind": ARTIFACT_KIND_CONTRADICTION_MAP,
        "study_id": study_id,
        "contradictions": pairs,
        "count": len(pairs),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return raw, sha256_hex(raw)
