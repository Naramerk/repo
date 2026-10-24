"""Unit tests for ``coscientist.tools.mcp_adapter`` (fake MCP, no network)."""

from __future__ import annotations

from typing import Any

import pytest

from coscientist.core.execution.domain import HitlPolicy
from coscientist.mcp.errors import McpError, McpTimeoutError, McpTransportError
from coscientist.mcp.types import McpTool
from coscientist.tools.contract import CallCtx, ToolError
from coscientist.tools.mcp_adapter import (
    McpBoundTool,
    bind_mcp_server_tools,
    tools_from_mcp_session,
)
from tests.mcp.conftest import make_client, make_config
from tests.mcp.fake_server import FakeStdioServer, default_handler


class _NoFetch:
    async def fetch(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("no fetch")


def _ctx() -> CallCtx:
    return CallCtx(
        study_id="st", task_key="T0", attempt_id="att#1", hitl_policy=HitlPolicy()
    )


def _kw(**extra: Any) -> dict[str, Any]:
    return {
        "server_name": "fake",
        "call_tool_timeout_s": 2.0,
        "effect": "read",
        "is_fetch": False,
        "idempotent": True,
        **extra,
    }


def test_mcp_bound_tool_init_spec_and_timeout_guard() -> None:
    with pytest.raises(ValueError, match="timeout_s must be > 0"):
        McpBoundTool(
            session=object(),  # type: ignore[arg-type]
            server_name="fake",
            mcp_tool=McpTool(name="echo", description=None, input_schema={}),
            timeout_s=0.0,
            effect="read",
            is_fetch=False,
            idempotent=True,
        )
    t = McpBoundTool(
        session=object(),  # type: ignore[arg-type]
        server_name="fake",
        mcp_tool=McpTool(name="echo", description="e", input_schema={"type": "object"}),
        timeout_s=2.0,
        effect="external_effect",
        is_fetch=True,
        idempotent=False,
    )
    assert t.server_name == "fake"
    assert (t.spec.provider, t.spec.effect, t.spec.is_fetch, t.spec.max_retries) == (
        "mcp",
        "external_effect",
        True,
        0,
    )
    assert t.description == "e"


@pytest.mark.asyncio
async def test_tools_from_session_call_errors_and_bind() -> None:
    async with FakeStdioServer(default_handler) as server:
        client = make_client(make_config(), file_fetcher=_NoFetch())
        session = await client.connect_memory("fake", server.client)
        try:
            with pytest.raises(ValueError, match="call_tool_timeout_s must be > 0"):
                await tools_from_mcp_session(session, **_kw(call_tool_timeout_s=0.0))
            tools = await tools_from_mcp_session(session, **_kw())
            assert [x.spec.name for x in tools] == ["echo"]
            out = await tools[0].call({"text": "ping"}, _ctx())
            assert out.data["result"]["content"][0]["text"] == "ping"
            assert out.data["content_digest"]
            assert out.data["files"] == []
        finally:
            await session.aclose()

    def err_h(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "x"}], "isError": True}
        return default_handler(method, params)

    async with FakeStdioServer(err_h) as server:
        client = make_client(make_config(), file_fetcher=_NoFetch())
        session = await client.connect_memory("fake", server.client)
        try:
            tools = await tools_from_mcp_session(session, **_kw())
            with pytest.raises(ToolError) as ei:
                await tools[0].call({"text": "x"}, _ctx())
            assert ei.value.kind == "opaque" and ei.value.retryable is False
        finally:
            await session.aclose()

    class _Fail:
        def __init__(self, exc: BaseException) -> None:
            self._exc = exc

        async def call_tool(self, *_a: Any, **_k: Any) -> Any:
            raise self._exc

    def bound(exc: BaseException) -> McpBoundTool:
        return McpBoundTool(
            session=_Fail(exc),  # type: ignore[arg-type]
            server_name="fake",
            mcp_tool=McpTool(name="echo", description=None, input_schema={}),
            timeout_s=1.0,
            effect="read",
            is_fetch=False,
            idempotent=True,
        )

    for exc, kind, retryable in (
        (McpTimeoutError("t"), "transport_sent", False),
        (McpTransportError("u", retryable=True), "transport_unsent", True),
        (McpTransportError("s", retryable=False), "transport_sent", False),
        (McpError("o"), "opaque", False),
    ):
        with pytest.raises(ToolError) as ei:
            await bound(exc).call({}, _ctx())
        assert (ei.value.kind, ei.value.retryable) == (kind, retryable)

    async with FakeStdioServer(default_handler) as server:
        client = make_client(make_config(), file_fetcher=_NoFetch())

        async def _connect(name: str) -> Any:
            return await client.connect_memory(name, server.client)

        client.connect = _connect  # type: ignore[method-assign]
        kw = _kw()
        session, tools = await bind_mcp_server_tools(client, kw.pop("server_name"), **kw)
        try:
            out = await tools[0].call({"text": "b"}, _ctx())
            assert out.data["result"]["content"][0]["text"] == "b"
        finally:
            await session.aclose()
