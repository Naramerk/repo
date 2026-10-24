"""ToolRouter(MCP): route calls via mcp_adapter (fake stdio + real stdio)."""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from coscientist.core.execution.domain import HitlPolicy
from coscientist.mcp.config import StdioServerConfig
from coscientist.mcp.errors import McpError
from coscientist.tools.contract import CallCtx
from coscientist.tools.mcp_adapter import McpAdapterError, McpToolProfile
from coscientist.tools.router import (
    McpServerRoute,
    ToolRouter,
    ToolRouterCloseError,
    ToolRouterConfig,
    ToolRouterError,
)
from tests.mcp.conftest import make_client, make_config
from tests.mcp.fake_server import FakeStdioServer, default_handler

# Literature-ish server ids used only in tests (not ToolRouter defaults).
SERVER_A = "papers-search"
SERVER_B = "paper-analysis"


class _NoFetch:
    async def fetch(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("no fetch")


def _ctx() -> CallCtx:
    return CallCtx(study_id="st", task_key="T0", attempt_id="att#1", hitl_policy=HitlPolicy())


def _profile(**extra: Any) -> McpToolProfile:
    base: dict[str, Any] = {
        "effect": "read",
        "is_fetch": False,
        "idempotent": True,
        "max_retries": 0,
    }
    base.update(extra)
    return McpToolProfile(**base)


def _route(
    server_name: str = SERVER_A,
    *,
    tools: dict[str, McpToolProfile] | None = None,
) -> McpServerRoute:
    return McpServerRoute(
        server_name=server_name,
        tools=tools if tools is not None else {"echo": _profile()},
    )


def _router_cfg(*routes: McpServerRoute) -> ToolRouterConfig:
    return ToolRouterConfig(servers=routes)


def _set_aclose(session: Any, fn: Callable[[], Awaitable[None]]) -> None:
    """Replace ``aclose`` without ``type: ignore`` (session typed as Any)."""
    session.aclose = fn


def _stdio_client(*names: str) -> Any:
    return make_client(
        make_config(
            servers=tuple(
                StdioServerConfig(
                    transport="stdio",
                    name=name,
                    command=sys.executable,
                    args=("-c", "pass"),
                    env_keys=(),
                )
                for name in names
            )
        ),
        file_fetcher=_NoFetch(),
    )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"server_name": "", "tools": {"echo": _profile()}}, "server_name"),
        ({"server_name": "bad name", "tools": {"echo": _profile()}}, "whitespace"),
        ({"server_name": "ok", "tools": {"": _profile()}}, "tool name"),
        ({"server_name": "ok", "tools": {"x y": _profile()}}, "whitespace"),
    ],
    ids=["empty-server", "ws-server", "empty-tool", "ws-tool"],
)
def test_server_route_refuses_bad_ids(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        McpServerRoute(**kwargs)


def test_router_config_refuses_duplicate_server() -> None:
    with pytest.raises(ValidationError, match="duplicate server_name"):
        ToolRouterConfig(servers=(_route(), _route()))


def test_router_config_refuses_empty_servers() -> None:
    with pytest.raises(ValidationError, match="servers"):
        ToolRouterConfig(servers=())


@pytest.mark.asyncio
async def test_router_call_via_fake_stdio() -> None:
    cfg = _router_cfg(_route(SERVER_A))
    router = ToolRouter(cfg)
    client = _stdio_client(SERVER_A)
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(SERVER_A, server.client)
        try:
            await router.attach_session(session, server_name=SERVER_A)
            out = await router.call(SERVER_A, "echo", {"text": "lit"}, _ctx())
            assert out.data["result"]["content"][0]["text"] == "lit"
            tool = router.require_tool(SERVER_A, "echo")
            assert tool.spec.name == f"{SERVER_A}:echo"
            assert tool.spec.provider == "mcp"
        finally:
            await router.aclose()


@pytest.mark.asyncio
async def test_router_unknown_server_fail_closed() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_A)))
    client = _stdio_client(SERVER_A)
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(SERVER_A, server.client)
        try:
            with pytest.raises(ToolRouterError, match="unknown server"):
                await router.attach_session(session, server_name=SERVER_B)
            with pytest.raises(ToolRouterError, match="not open"):
                router.require_tool(SERVER_A, "echo")
        finally:
            await session.aclose()


@pytest.mark.asyncio
async def test_router_unknown_tool_fail_closed() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_B)))
    client = _stdio_client(SERVER_B)
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(SERVER_B, server.client)
        try:
            await router.attach_session(session, server_name=SERVER_B)
            with pytest.raises(ToolRouterError, match="unknown tool"):
                router.require_tool(SERVER_B, "missing")
        finally:
            await router.aclose()


@pytest.mark.asyncio
async def test_router_open_refuses_server_missing_from_client() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_A)))
    client = _stdio_client("other-server")
    with pytest.raises(ToolRouterError, match="failed to bind"):
        await router.open(client)


@pytest.mark.asyncio
async def test_open_refuses_already_has_bindings() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_A)))
    client = _stdio_client(SERVER_A)
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(SERVER_A, server.client)
        try:
            await router.attach_session(session, server_name=SERVER_A)
            with pytest.raises(ToolRouterError, match="already has bindings"):
                await router.open(client)
        finally:
            await router.aclose()


@pytest.mark.asyncio
async def test_open_duplicate_mcp_tool_key() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_A), _route(SERVER_B)))
    client = _stdio_client(SERVER_A, SERVER_B)
    tool = AsyncMock()
    session = AsyncMock()
    session.aclose = AsyncMock()

    async def _bind(_client: Any, server_name: str, *, bindings: Any) -> Any:
        # Colliding key across servers → fail-closed duplicate.
        return session, {("dup", "echo"): tool}

    with patch("coscientist.tools.router.bind_mcp_server_tools", side_effect=_bind):
        with pytest.raises(ToolRouterError, match="duplicate MCP tool key"):
            await router.open(client)
    session.aclose.assert_awaited()


@pytest.mark.asyncio
async def test_attach_session_bind_failure() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_A, tools={"missing-tool": _profile()})))
    client = _stdio_client(SERVER_A)
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(SERVER_A, server.client)
        try:
            with pytest.raises(ToolRouterError, match="McpToolProfile|unknown tools|missing") as ei:
                await router.attach_session(session, server_name=SERVER_A)
            assert isinstance(ei.value.__cause__, (McpAdapterError, McpError))
        finally:
            await session.aclose()


@pytest.mark.asyncio
async def test_attach_session_duplicate_key_no_partial_write() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_A)))
    client = _stdio_client(SERVER_A)
    async with (
        FakeStdioServer(default_handler) as s1,
        FakeStdioServer(default_handler) as s2,
    ):
        sess1 = await client.connect_memory(SERVER_A, s1.client)
        sess2 = await client.connect_memory(SERVER_A, s2.client)
        try:
            await router.attach_session(sess1, server_name=SERVER_A)
            first = router.require_tool(SERVER_A, "echo")
            with pytest.raises(ToolRouterError, match="duplicate MCP tool key"):
                await router.attach_session(sess2, server_name=SERVER_A)
            assert router.require_tool(SERVER_A, "echo") is first
        finally:
            await router.aclose()
            await sess2.aclose()


@pytest.mark.asyncio
async def test_router_aclose_closes_all_then_raises() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_A), _route(SERVER_B)))
    client = _stdio_client(SERVER_A, SERVER_B)
    async with FakeStdioServer(default_handler) as s1, FakeStdioServer(default_handler) as s2:
        sess1 = await client.connect_memory(SERVER_A, s1.client)
        sess2 = await client.connect_memory(SERVER_B, s2.client)
        await router.attach_session(sess1, server_name=SERVER_A)
        await router.attach_session(sess2, server_name=SERVER_B)
        closed: list[str] = []

        async def _boom() -> None:
            closed.append("first")
            raise OSError("boom-close")

        async def _ok() -> None:
            closed.append("second")

        _set_aclose(sess1, _boom)
        _set_aclose(sess2, _ok)
        with pytest.raises(ToolRouterCloseError, match="session close failed \\(1\\)") as ei:
            await router.aclose()
        assert closed == ["first", "second"]
        assert isinstance(ei.value, ToolRouterError)
        assert isinstance(ei.value.__cause__, OSError)
        assert len(ei.value.causes) == 1 and isinstance(ei.value.causes[0], OSError)
        with pytest.raises(ToolRouterError, match="not open"):
            router.require_tool(SERVER_A, "echo")


@pytest.mark.asyncio
async def test_router_aclose_preserves_all_close_causes() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_A), _route(SERVER_B)))
    client = _stdio_client(SERVER_A, SERVER_B)
    async with FakeStdioServer(default_handler) as s1, FakeStdioServer(default_handler) as s2:
        sess1 = await client.connect_memory(SERVER_A, s1.client)
        sess2 = await client.connect_memory(SERVER_B, s2.client)
        await router.attach_session(sess1, server_name=SERVER_A)
        await router.attach_session(sess2, server_name=SERVER_B)

        async def _boom1() -> None:
            raise OSError("close-a")

        async def _boom2() -> None:
            raise TimeoutError("close-b")

        _set_aclose(sess1, _boom1)
        _set_aclose(sess2, _boom2)
        with pytest.raises(ToolRouterCloseError, match="session close failed \\(2\\)") as ei:
            await router.aclose()
        err = ei.value
        assert len(err.causes) == 2
        assert isinstance(err.causes[0], OSError) and str(err.causes[0]) == "close-a"
        assert isinstance(err.causes[1], TimeoutError) and str(err.causes[1]) == "close-b"
        assert "close-a" in str(err) and "close-b" in str(err)
        assert isinstance(err.__cause__, ExceptionGroup)
        assert [type(e) for e in err.__cause__.exceptions] == [OSError, TimeoutError]


def test_tool_router_close_error_requires_causes() -> None:
    with pytest.raises(TypeError, match="causes"):
        ToolRouterCloseError("x", **{})
    with pytest.raises(ValueError, match="non-empty"):
        ToolRouterCloseError("x", causes=())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [OSError("spawn-fail"), TimeoutError("connect-timeout")],
    ids=["oserror", "timeout"],
)
async def test_open_cleans_partial_binds_on_oserror_or_timeout(exc: BaseException) -> None:
    cfg = _router_cfg(_route(SERVER_A), _route(SERVER_B))
    router = ToolRouter(cfg)
    client = _stdio_client(SERVER_A, SERVER_B)
    closed = AsyncMock()
    first_session = AsyncMock()
    first_session.aclose = closed

    async def _bind(_client: Any, server_name: str, *, bindings: Any) -> Any:
        if server_name == SERVER_A:
            tool = AsyncMock()
            return first_session, {(SERVER_A, "echo"): tool}
        raise exc

    with patch("coscientist.tools.router.bind_mcp_server_tools", side_effect=_bind):
        with pytest.raises(type(exc), match=str(exc)):
            await router.open(client)
    closed.assert_awaited_once()
    with pytest.raises(ToolRouterError, match="not open"):
        router.require_tool(SERVER_A, "echo")


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
            text = req["params"]["arguments"]["text"]
            reply(req, {"content": [{"type": "text", "text": str(text)}], "isError": False})
        else:
            raise SystemExit(f"unexpected {method}")
    """
)


@pytest.mark.asyncio
async def test_router_open_real_stdio() -> None:
    client = make_client(
        make_config(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name=SERVER_A,
                    command=sys.executable,
                    args=("-u", "-c", _STDIO_ECHO),
                    env_keys=(),
                ),
            )
        ),
        file_fetcher=_NoFetch(),
    )
    router = ToolRouter(_router_cfg(_route(SERVER_A)))
    await router.open(client)
    try:
        out = await router.call(SERVER_A, "echo", {"text": "stdio"}, _ctx())
        assert out.data["result"]["content"][0]["text"] == "stdio"
    finally:
        await router.aclose()
