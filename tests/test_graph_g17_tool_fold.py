"""G17: ToolCalled → tool_call via G7 fold (#88 / TG-Call)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coscientist.core.claims.graph_fold.tool import tool_call_node_id, tool_called_graph_event
from coscientist.core.claims.graph_writer import GraphWriterError
from coscientist.core.events.events import UnstampedEvent
from coscientist.core.events.payload_schemas import ToolCalledPayload
from coscientist.core.events.projections import fold_events
from coscientist.core.events.schema_versions import TOOL_CALLED_SCHEMA_VERSION
from coscientist.core.execution.domain import ToolCallRef
from coscientist.schema import GRAPH_SCHEMA_VERSION


def _ts() -> datetime:
    return datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


def _ref(**kw: object) -> ToolCallRef:
    base: dict[str, object] = {
        "call_id": "att1:1",
        "tool": "echo",
        "args_digest": "deadbeef",
        "started_at": _ts(),
        "ended_at": _ts(),
    }
    base.update(kw)
    return ToolCallRef.model_validate(base)


def _ok(**kw: object) -> ToolCalledPayload:
    base: dict[str, object] = {
        "ref": _ref(),
        "outcome": "ok",
        "error_kind": None,
        "duration_ms": 12,
    }
    base.update(kw)
    return ToolCalledPayload.model_validate(base)


def _err(**kw: object) -> ToolCalledPayload:
    base: dict[str, object] = {
        "ref": _ref(),
        "outcome": "error",
        "error_kind": "ToolError",
        "duration_ms": 3,
    }
    base.update(kw)
    return ToolCalledPayload.model_validate(base)


def _stamp(study_id: str, payload: ToolCalledPayload) -> UnstampedEvent:
    return UnstampedEvent(
        study_id=study_id,
        type="ToolCalled",
        entity_key=payload.ref.call_id,
        idem_key=f"ToolCalled:{payload.ref.call_id}",
        payload=payload.model_dump(mode="json"),
        ts=_ts(),
        schema_version=TOOL_CALLED_SCHEMA_VERSION,
    )


def test_schema_version_and_kind_registered() -> None:
    assert GRAPH_SCHEMA_VERSION == "0.10"
    from coscientist.core.claims.graph import registry_from_schema

    reg = registry_from_schema()
    assert "tool_call" in reg.node_kinds()


def test_ok_fold_via_g7() -> None:
    sid = "st_g17"
    ev = tool_called_graph_event(study_id=sid, payload=_ok(), ts=_ts())
    assert ev.type == "GraphNodeUpserted"
    assert ev.entity_key == tool_call_node_id(sid, "att1:1")
    assert ev.payload["kind"] == "tool_call"
    attrs = ev.payload["attrs"]
    assert attrs["call_id"] == "att1:1"
    assert attrs["tool"] == "echo"
    assert attrs["args_digest"] == "deadbeef"
    assert attrs["outcome"] == "ok"
    assert attrs["duration_ms"] == 12
    assert "error_kind" not in attrs


def test_error_fold_includes_error_kind() -> None:
    sid = "st_g17"
    ev = tool_called_graph_event(study_id=sid, payload=_err(), ts=_ts())
    attrs = ev.payload["attrs"]
    assert attrs["outcome"] == "error"
    assert attrs["error_kind"] == "ToolError"


def test_fold_refuses_empty_ids() -> None:
    with pytest.raises(GraphWriterError, match="non-empty"):
        tool_call_node_id("", "c1")
    with pytest.raises(GraphWriterError, match="non-empty"):
        tool_call_node_id("st", "")


def test_projection_fold_ok_and_error() -> None:
    sid = "st_g17_proj"
    events = [
        _stamp(sid, _ok()),
        _stamp(sid, _err(ref=_ref(call_id="att1:2"))),
    ]
    proj = fold_events(events)
    ok_row = proj["graph_nodes"][tool_call_node_id(sid, "att1:1")]
    err_row = proj["graph_nodes"][tool_call_node_id(sid, "att1:2")]
    assert ok_row["kind"] == "tool_call"
    assert ok_row["attrs"]["outcome"] == "ok"
    assert "error_kind" not in ok_row["attrs"]
    assert err_row["attrs"]["outcome"] == "error"
    assert err_row["attrs"]["error_kind"] == "ToolError"
