"""G17: fold ToolCalled → tool_call via G7.

Attrs map 1:1 from TG-Call ``ToolCalledPayload`` (DESIGN §10), flattened from
``ref`` + outcome fields. ``error_kind`` only when ``outcome=error`` (no invent null).
"""

from __future__ import annotations

from datetime import datetime

from coscientist.core.claims.graph import GraphNode, KindRegistry, registry_from_schema
from coscientist.core.claims.graph_writer import GraphWriterError, graph_node_upserted
from coscientist.core.events.events import UnstampedEvent
from coscientist.core.events.payload_schemas import ToolCalledPayload


def _registry(registry: KindRegistry | None) -> KindRegistry:
    # registry_from_schema is lru_cached; no module-level None cache.
    if registry is not None:
        return registry
    return registry_from_schema()


def tool_call_node_id(study_id: str, call_id: str) -> str:
    if not study_id or not call_id:
        raise GraphWriterError("study_id and call_id must be non-empty")
    return f"{study_id}:tool_call:{call_id}"


def tool_called_graph_event(
    *,
    study_id: str,
    payload: ToolCalledPayload,
    ts: datetime,
    registry: KindRegistry | None = None,
) -> UnstampedEvent:
    """Build ``GraphNodeUpserted`` for a journaled ``ToolCalled``."""
    ref = payload.ref
    if ref.ended_at is None:
        raise GraphWriterError("tool_call fold requires ref.ended_at")
    attrs: dict[str, object] = {
        "call_id": ref.call_id,
        "tool": ref.tool,
        "args_digest": ref.args_digest,
        "started_at": ref.started_at,
        "ended_at": ref.ended_at,
        "outcome": payload.outcome,
        "duration_ms": payload.duration_ms,
    }
    if payload.outcome == "error":
        if type(payload.error_kind) is not str or not payload.error_kind:
            raise GraphWriterError("tool_call error fold requires non-empty error_kind")
        attrs["error_kind"] = payload.error_kind
    nid = tool_call_node_id(study_id, ref.call_id)
    node = GraphNode(
        node_id=nid,
        kind="tool_call",
        study_id=study_id,
        ref_id=ref.call_id,
        attrs=attrs,
        created_at=ts,
    )
    return graph_node_upserted(
        node=node,
        ts=ts,
        registry=_registry(registry),
        idem_extra=("tool_called",),
    )
