"""Unit tests for ``coscientist.tools.mcp_adapter`` (fake MCP / real stdio bind)."""

from __future__ import annotations

import sys
import textwrap
import tomllib
from pathlib import Path
from typing import Any

import pytest

from coscientist.core.execution.domain import HitlPolicy
from coscientist.mcp.config import StdioServerConfig
from coscientist.mcp.digest import content_digest
from coscientist.mcp.errors import McpError, McpTimeoutError, McpTransportError
from coscientist.mcp.types import McpTool
from coscientist.tools.contract import CallCtx, ToolError
from coscientist.tools.mcp_adapter import (
    McpAdapterError,
    McpBoundTool,
    McpToolProfile,
    bind_mcp_server_tools,
    mcp_tool_key,
    mcp_tool_spec_name,
    tools_from_mcp_session,
)
from tests.mcp.conftest import make_client, make_config
from tests.mcp.fake_server import FakeStdioServer, default_handler

ROOT = Path(__file__).resolve().parents[2]


class _NoFetch:
    async def fetch(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("no fetch")


class _StubSession:
    """Minimal session surface for error-mapping unit tests (no network)."""

    server_name = "fake"
    call_tool_timeout_s = 2.0

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def call_tool(self, *_a: Any, **_k: Any) -> Any:
        raise self._exc


def _ctx() -> CallCtx:
    return CallCtx(
        study_id="st", task_key="T0", attempt_id="att#1", hitl_policy=HitlPolicy()
    )


def _profile(**extra: Any) -> McpToolProfile:
    base: dict[str, Any] = {
        "effect": "read",
        "is_fetch": False,
        "idempotent": True,
        "max_retries": 0,
    }
    base.update(extra)
    return McpToolProfile(**base)


def _echo_bindings() -> dict[str, McpToolProfile]:
    return {"echo": _profile()}


def _bound(exc: BaseException) -> McpBoundTool:
    return McpBoundTool(
        session=_StubSession(exc),  # type: ignore[arg-type]
        server_name="fake",
        mcp_tool=McpTool(name="echo", description=None, input_schema={}),
        profile=_profile(),
    )


def test_mcp_tool_key_and_spec_name() -> None:
    assert mcp_tool_key("srv", "echo") == ("srv", "echo")
    assert mcp_tool_spec_name("srv", "echo") == "srv:echo"
    with pytest.raises(McpAdapterError, match="non-empty"):
        mcp_tool_key("", "echo")


def test_mcp_tool_profile_rejects_negative_max_retries() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="max_retries"):
        McpToolProfile(
            effect="read", is_fetch=False, idempotent=True, max_retries=-1
        )


@pytest.mark.asyncio
async def test_mcp_bound_tool_init_uses_session_call_tool_timeout_s() -> None:
    async with FakeStdioServer(default_handler) as server:
        client = make_client(
            make_config(call_tool_timeout_s=3.5), file_fetcher=_NoFetch()
        )
        session = await client.connect_memory("fake", server.client)
        try:
            tool = McpBoundTool(
                session=session,
                server_name="fake",
                mcp_tool=McpTool(
                    name="echo", description="e", input_schema={"type": "object"}
                ),
                profile=_profile(
                    effect="external_effect",
                    is_fetch=True,
                    idempotent=False,
                    max_retries=2,
                ),
            )
            assert tool.key == ("fake", "echo")
            assert tool.spec.name == "fake:echo"
            assert tool.spec.timeout_s == 3.5
            assert tool.spec.timeout_s == session.call_tool_timeout_s
            assert tool.spec.max_retries == 2
            assert tool.spec.effect == "external_effect"
            assert tool.spec.is_fetch is True
            assert tool.spec.idempotent is False
            assert tool.description == "e"
        finally:
            await session.aclose()


@pytest.mark.asyncio
async def test_mcp_bound_tool_call_returns_content_digest() -> None:
    expected = content_digest(
        {"content": [{"type": "text", "text": "ping"}], "isError": False}
    )
    async with FakeStdioServer(default_handler) as server:
        client = make_client(make_config(), file_fetcher=_NoFetch())
        session = await client.connect_memory("fake", server.client)
        try:
            tools = await tools_from_mcp_session(
                session, server_name="fake", bindings=_echo_bindings()
            )
            out = await tools[("fake", "echo")].call({"text": "ping"}, _ctx())
            assert out.data["result"]["content"][0]["text"] == "ping"
            assert out.data["content_digest"] == expected
            assert out.data["files"] == []
        finally:
            await session.aclose()


@pytest.mark.asyncio
async def test_mcp_bound_tool_call_is_error_includes_result_and_digest() -> None:
    err_result = {"content": [{"type": "text", "text": "boom"}], "isError": True}

    def err_h(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if method == "tools/call":
            return err_result
        return default_handler(method, params)

    async with FakeStdioServer(err_h) as server:
        client = make_client(make_config(), file_fetcher=_NoFetch())
        session = await client.connect_memory("fake", server.client)
        try:
            tools = await tools_from_mcp_session(
                session, server_name="fake", bindings=_echo_bindings()
            )
            with pytest.raises(ToolError, match="isError") as ei:
                await tools[("fake", "echo")].call({"text": "x"}, _ctx())
            assert ei.value.kind == "opaque"
            assert ei.value.retryable is False
            assert "isError" in ei.value.detail
            assert content_digest(err_result) in ei.value.detail
            assert "boom" in ei.value.detail
        finally:
            await session.aclose()


@pytest.mark.asyncio
async def test_mcp_bound_tool_maps_timeout_error() -> None:
    with pytest.raises(ToolError, match="slow") as ei:
        await _bound(McpTimeoutError("slow")).call({}, _ctx())
    assert ei.value.kind == "transport_sent"
    assert ei.value.retryable is False


@pytest.mark.asyncio
async def test_mcp_bound_tool_maps_transport_retryable() -> None:
    with pytest.raises(ToolError, match="reset") as ei:
        await _bound(McpTransportError("reset", retryable=True)).call({}, _ctx())
    assert ei.value.kind == "transport_unsent"
    assert ei.value.retryable is True


@pytest.mark.asyncio
async def test_mcp_bound_tool_maps_transport_unretryable() -> None:
    with pytest.raises(ToolError, match="gone") as ei:
        await _bound(McpTransportError("gone", retryable=False)).call({}, _ctx())
    assert ei.value.kind == "transport_sent"
    assert ei.value.retryable is False


@pytest.mark.asyncio
async def test_mcp_bound_tool_maps_mcp_error() -> None:
    with pytest.raises(ToolError, match="proto") as ei:
        await _bound(McpError("proto")).call({}, _ctx())
    assert ei.value.kind == "opaque"
    assert ei.value.retryable is False


@pytest.mark.asyncio
async def test_tools_from_mcp_session_returns_keyed_tools() -> None:
    async with FakeStdioServer(default_handler) as server:
        client = make_client(make_config(), file_fetcher=_NoFetch())
        session = await client.connect_memory("fake", server.client)
        try:
            tools = await tools_from_mcp_session(
                session, server_name="fake", bindings=_echo_bindings()
            )
            assert list(tools) == [("fake", "echo")]
            assert tools[("fake", "echo")].spec.name == "fake:echo"
        finally:
            await session.aclose()


@pytest.mark.asyncio
async def test_tools_from_mcp_session_rejects_server_name_mismatch() -> None:
    async with FakeStdioServer(default_handler) as server:
        client = make_client(make_config(), file_fetcher=_NoFetch())
        session = await client.connect_memory("fake", server.client)
        try:
            with pytest.raises(McpAdapterError, match="server_name"):
                await tools_from_mcp_session(
                    session, server_name="other", bindings=_echo_bindings()
                )
        finally:
            await session.aclose()


@pytest.mark.asyncio
async def test_tools_from_mcp_session_rejects_missing_binding() -> None:
    async with FakeStdioServer(default_handler) as server:
        client = make_client(make_config(), file_fetcher=_NoFetch())
        session = await client.connect_memory("fake", server.client)
        try:
            with pytest.raises(McpAdapterError, match="missing McpToolProfile"):
                await tools_from_mcp_session(session, server_name="fake", bindings={})
        finally:
            await session.aclose()


@pytest.mark.asyncio
async def test_tools_from_mcp_session_rejects_extra_binding() -> None:
    async with FakeStdioServer(default_handler) as server:
        client = make_client(make_config(), file_fetcher=_NoFetch())
        session = await client.connect_memory("fake", server.client)
        try:
            with pytest.raises(McpAdapterError, match="unknown tools"):
                await tools_from_mcp_session(
                    session,
                    server_name="fake",
                    bindings={"echo": _profile(), "ghost": _profile()},
                )
        finally:
            await session.aclose()


@pytest.mark.asyncio
async def test_tools_from_mcp_session_name_collision_across_servers() -> None:
    async with FakeStdioServer(default_handler) as s1, FakeStdioServer(
        default_handler
    ) as s2:
        c1 = make_client(
            make_config(
                servers=(
                    StdioServerConfig(
                        transport="stdio",
                        name="alpha",
                        command=sys.executable,
                        args=("-c", "pass"),
                        env_keys=(),
                    ),
                )
            ),
            file_fetcher=_NoFetch(),
        )
        c2 = make_client(
            make_config(
                servers=(
                    StdioServerConfig(
                        transport="stdio",
                        name="beta",
                        command=sys.executable,
                        args=("-c", "pass"),
                        env_keys=(),
                    ),
                )
            ),
            file_fetcher=_NoFetch(),
        )
        sess1 = await c1.connect_memory("alpha", s1.client)
        sess2 = await c2.connect_memory("beta", s2.client)
        try:
            t1 = await tools_from_mcp_session(
                sess1, server_name="alpha", bindings=_echo_bindings()
            )
            t2 = await tools_from_mcp_session(
                sess2, server_name="beta", bindings=_echo_bindings()
            )
            merged = {**t1, **t2}
            assert set(merged) == {("alpha", "echo"), ("beta", "echo")}
            assert merged[("alpha", "echo")].spec.name == "alpha:echo"
            assert merged[("beta", "echo")].spec.name == "beta:echo"
            out_a = await merged[("alpha", "echo")].call({"text": "A"}, _ctx())
            out_b = await merged[("beta", "echo")].call({"text": "B"}, _ctx())
            assert out_a.data["result"]["content"][0]["text"] == "A"
            assert out_b.data["result"]["content"][0]["text"] == "B"
        finally:
            await sess1.aclose()
            await sess2.aclose()


@pytest.mark.asyncio
async def test_tools_from_mcp_session_per_tool_profiles() -> None:
    def handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "read_t",
                        "description": None,
                        "inputSchema": {"type": "object"},
                    },
                    {
                        "name": "write_t",
                        "description": None,
                        "inputSchema": {"type": "object"},
                    },
                ]
            }
        return default_handler(method, params)

    bindings = {
        "read_t": _profile(effect="read", max_retries=0),
        "write_t": _profile(effect="write", idempotent=False, max_retries=1),
    }
    async with FakeStdioServer(handler) as server:
        client = make_client(make_config(), file_fetcher=_NoFetch())
        session = await client.connect_memory("fake", server.client)
        try:
            tools = await tools_from_mcp_session(
                session, server_name="fake", bindings=bindings
            )
            assert tools[("fake", "read_t")].spec.effect == "read"
            assert tools[("fake", "read_t")].spec.max_retries == 0
            assert tools[("fake", "write_t")].spec.effect == "write"
            assert tools[("fake", "write_t")].spec.idempotent is False
            assert tools[("fake", "write_t")].spec.max_retries == 1
        finally:
            await session.aclose()


_STDIO_ECHO = textwrap.dedent(
    """
    import json, sys
    def reply(req, result):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}) + "\\n")
        sys.stdout.flush()
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        req = json.loads(line)
        method = req.get("method")
        if method == "initialize":
            reply(req, {
                "protocolVersion": req["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo", "version": "0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(req, {"tools": [{
                "name": "echo",
                "description": "echo",
                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            }]})
        elif method == "tools/call":
            args = req["params"]["arguments"]
            text = args["text"]
            reply(req, {"content": [{"type": "text", "text": str(text)}], "isError": False})
        else:
            raise SystemExit(f"unexpected {method}")
    """
)


@pytest.mark.asyncio
async def test_bind_mcp_server_tools_connects_via_client() -> None:
    cfg = make_config(
        servers=(
            StdioServerConfig(
                transport="stdio",
                name="echo",
                command=sys.executable,
                args=("-u", "-c", _STDIO_ECHO),
                env_keys=(),
            ),
        )
    )
    client = make_client(cfg, file_fetcher=_NoFetch())
    session, tools = await bind_mcp_server_tools(
        client, "echo", bindings=_echo_bindings()
    )
    try:
        assert list(tools) == [("echo", "echo")]
        out = await tools[("echo", "echo")].call({"text": "bound"}, _ctx())
        assert out.data["result"]["content"][0]["text"] == "bound"
        assert tools[("echo", "echo")].spec.timeout_s == cfg.call_tool_timeout_s
    finally:
        await session.aclose()


def test_import_contract_allows_only_mcp_adapter() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    contract = next(
        c
        for c in data["tool"]["importlinter"]["contracts"]
        if c.get("name") == "agents, api, tools, llm must not import coscientist.mcp"
    )
    assert "coscientist.tools" in contract["source_modules"]
    assert contract["forbidden_modules"] == ["coscientist.mcp"]
    assert contract["ignore_imports"] == [
        "coscientist.tools.mcp_adapter -> coscientist.mcp.*"
    ]
    assert contract["unmatched_ignore_imports_alerting"] == "error"
