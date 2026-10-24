"""Research-graph journal writers (RESEARCH_MODEL §1): agents never emit nodes/edges.

Validates kinds/attrs/endpoints vs required KindRegistry; UnstampedEvent + digest
idem keys (created_at excluded).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from coscientist.core.claims.graph import (
    AttrFieldSpec,
    AttrType,
    GraphEdge,
    GraphNode,
    GraphSchemaError,
    KindRegistry,
    KindSpec,
    validate_attr_value,
)
from coscientist.core.events.digest import payload_digest
from coscientist.core.events.errors import CoreError
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.schema_gate import require_writer_schema_version


class GraphWriterError(CoreError):
    """Refuse invalid graph writes."""


def _require_nonempty(label: str, value: str) -> None:
    if not value:
        raise GraphWriterError(f"{label} must be non-empty")


def _field_null_ok(f: AttrFieldSpec) -> bool:
    """Null only when schema default is explicitly null (not optional⇒null)."""
    return f.default is not None and f.default.value is None


def _check_attr_value(t: AttrType, value: Any, *, name: str, null_ok: bool) -> None:
    msg = f"{name}: expected type {t.text}, got {type(value).__name__}"
    try:
        validate_attr_value(t, value, null_ok=null_ok, ctx=name)
    except GraphSchemaError as e:
        raise GraphWriterError(msg) from e


def _validated_attrs(*, kind: str, attrs: dict[str, Any], spec: KindSpec) -> dict[str, Any]:
    known = {f.name: f for f in spec.fields}
    out = dict(attrs)
    for f in spec.fields:
        if f.name not in out and f.default is not None:
            out[f.name] = f.default.value
    required = {f.name for f in spec.fields if f.required}
    missing = sorted(required - set(out))
    if missing:
        raise GraphWriterError(f"{kind} missing required attrs: {missing}")
    if not known:
        if out:
            raise GraphWriterError(f"{kind} has no schema fields but attrs were provided")
        return out
    unknown = sorted(set(out) - set(known))
    if unknown:
        raise GraphWriterError(f"{kind} unknown attrs: {unknown}")
    for fname, value in out.items():
        f = known[fname]
        _check_attr_value(f.type, value, name=fname, null_ok=_field_null_ok(f))
    return out


def _idem_digest(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "created_at"}
    return payload_digest(body)


def graph_node_upserted(
    *,
    node: GraphNode,
    ts: datetime,
    registry: KindRegistry,
    idem_extra: tuple[str, ...],
) -> UnstampedEvent:
    """Build a ``GraphNodeUpserted`` ``UnstampedEvent`` for ``node``."""
    study_id = node.study_id
    _require_nonempty("node.study_id", study_id)
    _require_nonempty("node.node_id", node.node_id)
    _require_nonempty("node.kind", node.kind)
    if node.kind not in registry.node_kinds():
        raise GraphWriterError(f"unknown node kind: {node.kind}")
    spec = registry.require_node_kind(node.kind)
    attrs = _validated_attrs(kind=node.kind, attrs=node.attrs, spec=spec)
    payload = node.model_copy(update={"attrs": attrs}).model_dump(mode="json")
    digest = _idem_digest(payload)
    version = require_writer_schema_version("GraphNodeUpserted")
    return UnstampedEvent(
        study_id=study_id,
        type="GraphNodeUpserted",
        entity_key=node.node_id,
        idem_key=make_idem("GraphNodeUpserted", node.node_id, digest, *idem_extra),
        payload=payload,
        ts=ts,
        schema_version=version,
    )


def graph_edge_added(
    *,
    edge: GraphEdge,
    ts: datetime,
    src: GraphNode,
    dst: GraphNode,
    registry: KindRegistry,
    idem_extra: tuple[str, ...],
) -> UnstampedEvent:
    """Build ``GraphEdgeAdded``; ``src``/``dst`` must match edge endpoints (GraphStore-style)."""
    study_id = edge.study_id
    _require_nonempty("edge.study_id", study_id)
    _require_nonempty("edge.edge_id", edge.edge_id)
    _require_nonempty("edge.kind", edge.kind)
    _require_nonempty("edge.src_id", edge.src_id)
    _require_nonempty("edge.dst_id", edge.dst_id)
    _require_nonempty("src.node_id", src.node_id)
    _require_nonempty("src.kind", src.kind)
    _require_nonempty("dst.node_id", dst.node_id)
    _require_nonempty("dst.kind", dst.kind)
    if src.node_id != edge.src_id:
        raise GraphWriterError(f"src.node_id {src.node_id!r} != edge.src_id {edge.src_id!r}")
    if dst.node_id != edge.dst_id:
        raise GraphWriterError(f"dst.node_id {dst.node_id!r} != edge.dst_id {edge.dst_id!r}")
    if src.study_id != study_id:
        raise GraphWriterError(f"src.study_id {src.study_id!r} != edge.study_id {study_id!r}")
    if dst.study_id != study_id:
        raise GraphWriterError(f"dst.study_id {dst.study_id!r} != edge.study_id {study_id!r}")
    if edge.kind not in registry.edge_kinds():
        raise GraphWriterError(f"unknown edge kind: {edge.kind}")
    if src.kind not in registry.node_kinds():
        raise GraphWriterError(f"unknown node kind: {src.kind}")
    if dst.kind not in registry.node_kinds():
        raise GraphWriterError(f"unknown node kind: {dst.kind}")
    edge_spec = registry.require_edge_kind(edge.kind)
    if not registry.edge_allowed(edge.kind, src.kind, dst.kind):
        raise GraphWriterError(f"edge {edge.kind!r}: {src.kind} -> {dst.kind} not allowed")
    attrs = _validated_attrs(kind=edge.kind, attrs=edge.attrs, spec=edge_spec)
    payload = edge.model_copy(update={"attrs": attrs}).model_dump(mode="json")
    digest = _idem_digest(payload)
    version = require_writer_schema_version("GraphEdgeAdded")
    return UnstampedEvent(
        study_id=study_id,
        type="GraphEdgeAdded",
        entity_key=edge.edge_id,
        idem_key=make_idem("GraphEdgeAdded", edge.edge_id, digest, *idem_extra),
        payload=payload,
        ts=ts,
        schema_version=version,
    )
