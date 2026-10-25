"""MCP-J1 (#78): McpRequestRecorded / McpResponseRecorded via mcp_adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.events import EventType, UnstampedEvent
from coscientist.core.events.mcp_events import (
    McpEventType,
    McpRequestRecordedPayload,
    McpResponseRecordedPayload,
)
from coscientist.core.events.payload_schemas import PAYLOAD_MODELS, validate_event_payload
from coscientist.core.events.schema_gate import _payload_validators
from coscientist.core.events.schema_versions import MCP_EVENTS_SCHEMA_VERSION
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.execution.domain import HitlPolicy
from coscientist.mcp.digest import content_digest
from coscientist.mcp.errors import McpTimeoutError
from coscientist.mcp.types import CallToolReply, McpTool
from coscientist.tools.contract import CallCtx, ToolError
from coscientist.tools.mcp_adapter import McpBoundTool, McpToolProfile


class _Journal:
    def __init__(self) -> None:
        self.events: list[UnstampedEvent] = []

    async def record(self, event: UnstampedEvent) -> None:
        self.events.append(event)


class _StubSession:
    server_name = "fake"
    call_tool_timeout_s = 2.0

    def __init__(self, reply: CallToolReply | BaseException) -> None:
        self._reply = reply

    async def call_tool(self, *_a: object, **_k: object) -> CallToolReply:
        if isinstance(self._reply, BaseException):
            raise self._reply
        return self._reply


def _ctx() -> CallCtx:
    return CallCtx(
        study_id="st_mcp",
        task_key="T0",
        attempt_id="att#1",
        hitl_policy=HitlPolicy(),
        call_seq=1,
    )


def _profile() -> McpToolProfile:
    return McpToolProfile(effect="read", is_fetch=False, idempotent=True, max_retries=0)


def _reply(*, is_error: bool = False) -> CallToolReply:
    result = {"isError": is_error, "content": [{"type": "text", "text": "ok"}]}
    return CallToolReply(
        content_digest=content_digest(result),
        raw_bytes=b"{}",
        result=result,
        files=(),
        is_error=is_error,
    )


def _bound(reply: CallToolReply | BaseException, journal: _Journal | None) -> McpBoundTool:
    return McpBoundTool(
        session=_StubSession(reply),  # type: ignore[arg-type]
        server_name="fake",
        mcp_tool=McpTool(name="echo", description=None, input_schema={}),
        profile=_profile(),
        journal=journal,
    )


def test_mcp_event_types_registered() -> None:
    known = set(get_args(EventType))
    for name in get_args(McpEventType):
        assert name in known
    assert set(PAYLOAD_MODELS) == set(get_args(EventType)) == set(_payload_validators())
    assert PAYLOAD_MODELS["McpRequestRecorded"] is McpRequestRecordedPayload
    assert PAYLOAD_MODELS["McpResponseRecorded"] is McpResponseRecordedPayload


def test_payload_refuse_blank_extra_and_bool() -> None:
    base = {
        "call_id": "att#1:1",
        "server_name": "srv",
        "tool": "echo",
        "attempt_id": "att#1",
        "args_digest": "a" * 64,
    }
    assert validate_event_payload("McpRequestRecorded", base) is base
    with pytest.raises(ValidationError, match="surrounding whitespace|non-empty"):
        McpRequestRecordedPayload(**{**base, "tool": "  "})
    with pytest.raises(ValidationError):
        McpRequestRecordedPayload.model_validate({**base, "extra": 1})
    resp = {
        "call_id": "att#1:1",
        "server_name": "srv",
        "tool": "echo",
        "attempt_id": "att#1",
        "content_digest": "b" * 64,
        "is_error": False,
    }
    assert validate_event_payload("McpResponseRecorded", resp) is resp
    with pytest.raises(ValidationError):
        McpResponseRecordedPayload(**{**resp, "is_error": "no"})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_journals_ok_is_error_and_transport() -> None:
    j_ok = _Journal()
    out = await _bound(_reply(), j_ok).call({"q": 1}, _ctx())
    assert [e.type for e in j_ok.events] == ["McpRequestRecorded", "McpResponseRecorded"]
    assert j_ok.events[0].schema_version == MCP_EVENTS_SCHEMA_VERSION
    assert j_ok.events[0].entity_key == "att#1:1"
    rp = validate_event_payload("McpRequestRecorded", j_ok.events[0].payload)
    assert rp["args_digest"] == content_digest({"q": 1}) and rp["tool"] == "echo"
    sp = validate_event_payload("McpResponseRecorded", j_ok.events[1].payload)
    assert sp["is_error"] is False and sp["content_digest"] == out.data["content_digest"]

    j_err = _Journal()
    with pytest.raises(ToolError, match="isError"):
        await _bound(_reply(is_error=True), j_err).call({}, _ctx())
    assert [e.type for e in j_err.events] == ["McpRequestRecorded", "McpResponseRecorded"]
    assert j_err.events[1].payload["is_error"] is True

    j_to = _Journal()
    with pytest.raises(ToolError, match="timeout"):
        await _bound(McpTimeoutError("timeout"), j_to).call({}, _ctx())
    assert [e.type for e in j_to.events] == ["McpRequestRecorded"]


@pytest.mark.asyncio
async def test_store_append_roundtrip(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 25, tzinfo=UTC))
    store = SqliteEventStore("st_mcp", tmp_path / "j.db", clock=clock)
    await store.acquire_lease("o")

    class _StoreJournal:
        async def record(self, event: UnstampedEvent) -> None:
            await store.append(event)

    await _bound(_reply(), _StoreJournal()).call({"msg": "hi"}, _ctx())  # type: ignore[arg-type]
    evs = await store.read_all()
    assert [e.type for e in evs] == ["McpRequestRecorded", "McpResponseRecorded"]
    for ev in evs:
        validate_event_payload(ev.type, ev.payload)
