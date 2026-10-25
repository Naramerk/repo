"""G18: fold McpRequestRecorded / McpResponseRecorded → mcp_exchange via G7.

Attrs map 1:1 from MCP-J1 payloads. Response merges onto the request node
(same ``call_id``); missing prior request → typed refuse (no invent args_digest).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from coscientist.core.claims.graph import GraphNode, KindRegistry, registry_from_schema
from coscientist.core.claims.graph_writer import GraphWriterError, graph_node_upserted
from coscientist.core.events.events import UnstampedEvent
from coscientist.core.events.mcp_events import (
    McpRequestRecordedPayload,
    McpResponseRecordedPayload,
)


def _registry(registry: KindRegistry | None) -> KindRegistry:
    # registry_from_schema is lru_cached; no module-level None cache.
    if registry is not None:
        return registry
    return registry_from_schema()


def mcp_exchange_node_id(study_id: str, call_id: str) -> str:
    if not study_id or not call_id:
        raise GraphWriterError("study_id and call_id must be non-empty")
    return f"{study_id}:mcp_exchange:{call_id}"


def mcp_request_graph_event(
    *,
    study_id: str,
    payload: McpRequestRecordedPayload,
    ts: datetime,
    registry: KindRegistry | None = None,
) -> UnstampedEvent:
    """Build ``GraphNodeUpserted`` for a request (response attrs absent)."""
    nid = mcp_exchange_node_id(study_id, payload.call_id)
    node = GraphNode(
        node_id=nid,
        kind="mcp_exchange",
        study_id=study_id,
        ref_id=payload.call_id,
        attrs={
            "call_id": payload.call_id,
            "server_name": payload.server_name,
            "tool": payload.tool,
            "attempt_id": payload.attempt_id,
            "args_digest": payload.args_digest,
        },
        created_at=ts,
    )
    return graph_node_upserted(
        node=node,
        ts=ts,
        registry=_registry(registry),
        idem_extra=("mcp_request",),
    )


def mcp_response_graph_event(
    *,
    study_id: str,
    payload: McpResponseRecordedPayload,
    ts: datetime,
    prior_attrs: dict[str, Any] | None,
    registry: KindRegistry | None = None,
) -> UnstampedEvent:
    """Build ``GraphNodeUpserted`` merging response onto prior request attrs.

    ``prior_attrs`` must carry ``args_digest`` from the request fold — never invent.
    """
    if not prior_attrs:
        raise GraphWriterError(
            "mcp_exchange response fold requires prior request attrs (args_digest)"
        )
    args_digest = prior_attrs.get("args_digest")
    if type(args_digest) is not str or not args_digest:
        raise GraphWriterError(
            "mcp_exchange response fold requires prior args_digest from request"
        )
    nid = mcp_exchange_node_id(study_id, payload.call_id)
    node = GraphNode(
        node_id=nid,
        kind="mcp_exchange",
        study_id=study_id,
        ref_id=payload.call_id,
        attrs={
            "call_id": payload.call_id,
            "server_name": payload.server_name,
            "tool": payload.tool,
            "attempt_id": payload.attempt_id,
            "args_digest": args_digest,
            "content_digest": payload.content_digest,
            "is_error": payload.is_error,
        },
        created_at=ts,
    )
    return graph_node_upserted(
        node=node,
        ts=ts,
        registry=_registry(registry),
        idem_extra=("mcp_response",),
    )
