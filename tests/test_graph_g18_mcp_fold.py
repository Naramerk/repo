"""G18: Mcp*Recorded → mcp_exchange via G7 fold (#89 / MCP-J1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coscientist.core.claims.graph_fold.mcp import (
    mcp_exchange_node_id,
    mcp_request_graph_event,
    mcp_response_graph_event,
)
from coscientist.core.claims.graph_writer import GraphWriterError
from coscientist.core.events.events import UnstampedEvent
from coscientist.core.events.mcp_events import (
    McpRequestRecordedPayload,
    McpResponseRecordedPayload,
)
from coscientist.core.events.projections import apply_to_map, fold_events
from coscientist.core.events.schema_versions import MCP_EVENTS_SCHEMA_VERSION
from coscientist.schema import GRAPH_SCHEMA_VERSION


def _ts() -> datetime:
    return datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


def _req(**kw: object) -> McpRequestRecordedPayload:
    base = {
        "call_id": "att1:1",
        "server_name": "srv",
        "tool": "echo",
        "attempt_id": "att1",
        "args_digest": "deadbeef",
    }
    base.update(kw)
    return McpRequestRecordedPayload.model_validate(base)


def _resp(**kw: object) -> McpResponseRecordedPayload:
    base = {
        "call_id": "att1:1",
        "server_name": "srv",
        "tool": "echo",
        "attempt_id": "att1",
        "content_digest": "cafebabe",
        "is_error": False,
    }
    base.update(kw)
    return McpResponseRecordedPayload.model_validate(base)


def _stamp(study_id: str, typ: str, payload: object, *, call_id: str) -> UnstampedEvent:
    return UnstampedEvent(
        study_id=study_id,
        type=typ,  # type: ignore[arg-type]
        entity_key=call_id,
        idem_key=f"{typ}:{call_id}",
        payload=payload.model_dump(mode="json"),  # type: ignore[union-attr]
        ts=_ts(),
        schema_version=MCP_EVENTS_SCHEMA_VERSION,
    )


def test_schema_version_and_kind_registered() -> None:
    assert GRAPH_SCHEMA_VERSION == "0.7"
    from coscientist.core.claims.graph import registry_from_schema

    reg = registry_from_schema()
    assert "mcp_exchange" in reg.node_kinds()


def test_request_fold_via_g7() -> None:
    sid = "st_g18"
    ev = mcp_request_graph_event(study_id=sid, payload=_req(), ts=_ts())
    assert ev.type == "GraphNodeUpserted"
    assert ev.entity_key == mcp_exchange_node_id(sid, "att1:1")
    assert ev.payload["kind"] == "mcp_exchange"
    assert ev.payload["attrs"]["args_digest"] == "deadbeef"
    assert "content_digest" not in ev.payload["attrs"]
    assert "is_error" not in ev.payload["attrs"]


def test_response_fold_requires_prior_args_digest() -> None:
    with pytest.raises(GraphWriterError, match="prior"):
        mcp_response_graph_event(
            study_id="st_g18",
            payload=_resp(),
            ts=_ts(),
            prior_attrs=None,
        )
    with pytest.raises(GraphWriterError, match="args_digest"):
        mcp_response_graph_event(
            study_id="st_g18",
            payload=_resp(),
            ts=_ts(),
            prior_attrs={"call_id": "att1:1"},
        )


def test_response_fold_merges_request_attrs() -> None:
    sid = "st_g18"
    req_ev = mcp_request_graph_event(study_id=sid, payload=_req(), ts=_ts())
    resp_ev = mcp_response_graph_event(
        study_id=sid,
        payload=_resp(is_error=True),
        ts=_ts(),
        prior_attrs=dict(req_ev.payload["attrs"]),
    )
    attrs = resp_ev.payload["attrs"]
    assert attrs["args_digest"] == "deadbeef"
    assert attrs["content_digest"] == "cafebabe"
    assert attrs["is_error"] is True


def test_projection_fold_request_then_response() -> None:
    sid = "st_g18_proj"
    events = [
        _stamp(sid, "McpRequestRecorded", _req(), call_id="att1:1"),
        _stamp(sid, "McpResponseRecorded", _resp(), call_id="att1:1"),
    ]
    proj = fold_events(events)
    nid = mcp_exchange_node_id(sid, "att1:1")
    row = proj["graph_nodes"][nid]
    assert row["kind"] == "mcp_exchange"
    assert row["attrs"]["args_digest"] == "deadbeef"
    assert row["attrs"]["content_digest"] == "cafebabe"
    assert row["attrs"]["is_error"] is False


def test_projection_request_only_timeout() -> None:
    sid = "st_g18_to"
    proj = fold_events([_stamp(sid, "McpRequestRecorded", _req(), call_id="att1:1")])
    nid = mcp_exchange_node_id(sid, "att1:1")
    attrs = proj["graph_nodes"][nid]["attrs"]
    assert attrs["args_digest"] == "deadbeef"
    assert "content_digest" not in attrs


def test_projection_response_without_request_skips() -> None:
    sid = "st_g18_orphan"
    proj: dict = {}
    apply_to_map(proj, _stamp(sid, "McpResponseRecorded", _resp(), call_id="att1:1"))
    assert "graph_nodes" not in proj or mcp_exchange_node_id(sid, "att1:1") not in proj.get(
        "graph_nodes", {}
    )
