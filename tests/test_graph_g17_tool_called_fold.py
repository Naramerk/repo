"""G17: ToolCalled → tool_call via G7 fold (#88 / TG-Call)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coscientist.core.claims.graph_fold.tool_called import (
    tool_call_node_id,
    tool_called_graph_event,
)
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
        "call_id": "a#1:1",
        "tool": "catalog_read",
        "args_digest": "d" * 64,
        "started_at": _ts(),
        "ended_at": _ts(),
    }
    base.update(kw)
    return ToolCallRef.model_validate(base)


def _payload(
    *,
    outcome: str = "ok",
    error_kind: str | None = None,
    duration_ms: int = 12,
    **ref_kw: object,
) -> ToolCalledPayload:
    return ToolCalledPayload.model_validate(
        {
            "ref": _ref(**ref_kw),
            "outcome": outcome,
            "error_kind": error_kind,
            "duration_ms": duration_ms,
        }
    )


def _stamp(study_id: str, payload: ToolCalledPayload) -> UnstampedEvent:
    return UnstampedEvent(
        study_id=study_id,
        type="ToolCalled",  # type: ignore[arg-type]
        entity_key=payload.ref.call_id,
        idem_key=f"ToolCalled:{payload.ref.call_id}",
        payload=payload.model_dump(mode="json"),
        ts=_ts(),
        schema_version=TOOL_CALLED_SCHEMA_VERSION,
    )


def test_schema_version_and_kind_registered() -> None:
    assert GRAPH_SCHEMA_VERSION == "0.7"
    from coscientist.core.claims.graph import registry_from_schema

    reg = registry_from_schema()
    assert "tool_call" in reg.node_kinds()


def test_ok_fold_via_g7_omits_error_kind() -> None:
    sid = "st_g17"
    ev = tool_called_graph_event(study_id=sid, payload=_payload(), ts=_ts())
    assert ev.type == "GraphNodeUpserted"
    assert ev.entity_key == tool_call_node_id(sid, "a#1:1")
    assert ev.payload["kind"] == "tool_call"
    attrs = ev.payload["attrs"]
    assert attrs["call_id"] == "a#1:1"
    assert attrs["tool"] == "catalog_read"
    assert attrs["args_digest"] == "d" * 64
    assert attrs["outcome"] == "ok"
    assert attrs["duration_ms"] == 12
    assert "error_kind" not in attrs


def test_error_fold_requires_error_kind() -> None:
    with pytest.raises(ValidationError):
        _payload(outcome="error", error_kind=None)
    sid = "st_g17"
    ev = tool_called_graph_event(
        study_id=sid,
        payload=_payload(outcome="error", error_kind="local"),
        ts=_ts(),
    )
    assert ev.payload["attrs"]["outcome"] == "error"
    assert ev.payload["attrs"]["error_kind"] == "local"


def test_node_id_refuses_empty() -> None:
    with pytest.raises(GraphWriterError, match="non-empty"):
        tool_call_node_id("", "a#1:1")
    with pytest.raises(GraphWriterError, match="non-empty"):
        tool_call_node_id("st", "")


def test_projection_fold_ok_and_error() -> None:
    sid = "st_g17_proj"
    ok = _payload(call_id="a#1:1")
    err = _payload(call_id="a#1:2", outcome="error", error_kind="local")
    proj = fold_events([_stamp(sid, ok), _stamp(sid, err)])
    ok_row = proj["graph_nodes"][tool_call_node_id(sid, "a#1:1")]
    err_row = proj["graph_nodes"][tool_call_node_id(sid, "a#1:2")]
    assert ok_row["kind"] == "tool_call"
    assert "error_kind" not in ok_row["attrs"]
    assert err_row["attrs"]["error_kind"] == "local"
