"""ToolRouter(MCP): route calls via mcp_adapter (fake stdio + real stdio)."""

from __future__ import annotations

import sys
import textwrap
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from coscientist.core.execution.domain import HitlPolicy
from coscientist.mcp.config import StdioServerConfig
from coscientist.tools.contract import CallCtx
from coscientist.tools.mcp_adapter import McpToolProfile
from coscientist.tools.router import (
    MCP_SERVER_PAPER_ANALYSIS,
    MCP_SERVER_PAPERS_SEARCH,
    McpServerRoute,
    ToolRouter,
    ToolRouterConfig,
    ToolRouterError,
)
from tests.mcp.conftest import make_client, make_config
from tests.mcp.fake_server import FakeStdioServer, default_handler


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
    server_name: str = MCP_SERVER_PAPERS_SEARCH,
    *,
    tools: dict[str, McpToolProfile] | None = None,
) -> McpServerRoute:
    return McpServerRoute(
        server_name=server_name,
        tools=tools if tools is not None else {"echo": _profile()},
    )


def _router_cfg(*routes: McpServerRoute) -> ToolRouterConfig:
    return ToolRouterConfig(servers=routes)


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
async def test_router_call_via_fake_stdio_papers_search() -> None:
    cfg = _router_cfg(_route(MCP_SERVER_PAPERS_SEARCH))
    router = ToolRouter(cfg)
    client = make_client(
        make_config(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name=MCP_SERVER_PAPERS_SEARCH,
                    command=sys.executable,
                    args=("-c", "pass"),
                    env_keys=(),
                ),
            )
        ),
        file_fetcher=_NoFetch(),
    )
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(MCP_SERVER_PAPERS_SEARCH, server.client)
        try:
            await router.attach_session(session, server_name=MCP_SERVER_PAPERS_SEARCH)
            out = await router.call(MCP_SERVER_PAPERS_SEARCH, "echo", {"text": "lit"}, _ctx())
            assert out.data["result"]["content"][0]["text"] == "lit"
            tool = router.require_tool(MCP_SERVER_PAPERS_SEARCH, "echo")
            assert tool.spec.name == f"{MCP_SERVER_PAPERS_SEARCH}:echo"
            assert tool.spec.provider == "mcp"
        finally:
            await router.aclose()


@pytest.mark.asyncio
async def test_router_unknown_server_fail_closed() -> None:
    router = ToolRouter(_router_cfg(_route(MCP_SERVER_PAPERS_SEARCH)))
    client = make_client(
        make_config(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name=MCP_SERVER_PAPERS_SEARCH,
                    command=sys.executable,
                    args=("-c", "pass"),
                    env_keys=(),
                ),
            )
        ),
        file_fetcher=_NoFetch(),
    )
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(MCP_SERVER_PAPERS_SEARCH, server.client)
        try:
            with pytest.raises(ToolRouterError, match="unknown server"):
                await router.attach_session(session, server_name=MCP_SERVER_PAPER_ANALYSIS)
            with pytest.raises(ToolRouterError, match="not open"):
                router.require_tool(MCP_SERVER_PAPERS_SEARCH, "echo")
        finally:
            await session.aclose()


@pytest.mark.asyncio
async def test_router_unknown_tool_fail_closed() -> None:
    cfg = _router_cfg(_route(MCP_SERVER_PAPER_ANALYSIS))
    router = ToolRouter(cfg)
    client = make_client(
        make_config(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name=MCP_SERVER_PAPER_ANALYSIS,
                    command=sys.executable,
                    args=("-c", "pass"),
                    env_keys=(),
                ),
            )
        ),
        file_fetcher=_NoFetch(),
    )
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(MCP_SERVER_PAPER_ANALYSIS, server.client)
        try:
            await router.attach_session(session, server_name=MCP_SERVER_PAPER_ANALYSIS)
            with pytest.raises(ToolRouterError, match="unknown tool"):
                router.require_tool(MCP_SERVER_PAPER_ANALYSIS, "missing")
        finally:
            await router.aclose()


@pytest.mark.asyncio
async def test_router_open_refuses_server_missing_from_client() -> None:
    router = ToolRouter(_router_cfg(_route(MCP_SERVER_PAPERS_SEARCH)))
    client = make_client(
        make_config(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name="other-server",
                    command=sys.executable,
                    args=("-c", "pass"),
                    env_keys=(),
                ),
            )
        ),
        file_fetcher=_NoFetch(),
    )
    with pytest.raises(ToolRouterError, match="failed to bind"):
        await router.open(client)


@pytest.mark.asyncio
async def test_router_aclose_closes_all_then_raises() -> None:
    """First session close fails; second still closes; then typed ToolRouterError."""
    cfg = _router_cfg(
        _route(MCP_SERVER_PAPERS_SEARCH),
        _route(MCP_SERVER_PAPER_ANALYSIS),
    )
    router = ToolRouter(cfg)
    client = make_client(
        make_config(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name=MCP_SERVER_PAPERS_SEARCH,
                    command=sys.executable,
                    args=("-c", "pass"),
                    env_keys=(),
                ),
                StdioServerConfig(
                    transport="stdio",
                    name=MCP_SERVER_PAPER_ANALYSIS,
                    command=sys.executable,
                    args=("-c", "pass"),
                    env_keys=(),
                ),
            )
        ),
        file_fetcher=_NoFetch(),
    )
    async with (
        FakeStdioServer(default_handler) as s1,
        FakeStdioServer(default_handler) as s2,
    ):
        sess1 = await client.connect_memory(MCP_SERVER_PAPERS_SEARCH, s1.client)
        sess2 = await client.connect_memory(MCP_SERVER_PAPER_ANALYSIS, s2.client)
        await router.attach_session(sess1, server_name=MCP_SERVER_PAPERS_SEARCH)
        await router.attach_session(sess2, server_name=MCP_SERVER_PAPER_ANALYSIS)
        closed: list[str] = []

        async def _boom() -> None:
            closed.append("first")
            raise OSError("boom-close")

        async def _ok() -> None:
            closed.append("second")

        sess1.aclose = _boom  # type: ignore[method-assign]
        sess2.aclose = _ok  # type: ignore[method-assign]
        with pytest.raises(ToolRouterError, match="session close failed \\(1\\)") as ei:
            await router.aclose()
        assert closed == ["first", "second"]
        assert isinstance(ei.value.__cause__, OSError)
        with pytest.raises(ToolRouterError, match="not open"):
            router.require_tool(MCP_SERVER_PAPERS_SEARCH, "echo")


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [OSError("spawn-fail"), TimeoutError("connect-timeout")])
async def test_open_cleans_partial_binds_on_oserror_or_timeout(exc: BaseException) -> None:
    """Any escape from bind (not only ToolRouterError) still acloses prior sessions."""
    cfg = _router_cfg(
        _route(MCP_SERVER_PAPERS_SEARCH),
        _route(MCP_SERVER_PAPER_ANALYSIS),
    )
    router = ToolRouter(cfg)
    client = make_client(
        make_config(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name=MCP_SERVER_PAPERS_SEARCH,
                    command=sys.executable,
                    args=("-c", "pass"),
                    env_keys=(),
                ),
                StdioServerConfig(
                    transport="stdio",
                    name=MCP_SERVER_PAPER_ANALYSIS,
                    command=sys.executable,
                    args=("-c", "pass"),
                    env_keys=(),
                ),
            )
        ),
        file_fetcher=_NoFetch(),
    )
    closed = AsyncMock()
    first_session = AsyncMock()
    first_session.aclose = closed

    async def _bind(_client: Any, server_name: str, *, bindings: Any) -> Any:
        if server_name == MCP_SERVER_PAPERS_SEARCH:
            tool = AsyncMock()
            return first_session, {(MCP_SERVER_PAPERS_SEARCH, "echo"): tool}
        raise exc

    with patch("coscientist.tools.router.bind_mcp_server_tools", side_effect=_bind):
        with pytest.raises(type(exc), match=str(exc)):
            await router.open(client)
    closed.assert_awaited_once()
    with pytest.raises(ToolRouterError, match="not open"):
        router.require_tool(MCP_SERVER_PAPERS_SEARCH, "echo")


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
                "serverInfo": {"name": "papers-search", "version": "0"},
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
async def test_router_open_real_stdio_papers_search() -> None:
    client = make_client(
        make_config(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name=MCP_SERVER_PAPERS_SEARCH,
                    command=sys.executable,
                    args=("-u", "-c", _STDIO_ECHO),
                    env_keys=(),
                ),
            )
        ),
        file_fetcher=_NoFetch(),
    )
    router = ToolRouter(_router_cfg(_route(MCP_SERVER_PAPERS_SEARCH)))
    await router.open(client)
    try:
        out = await router.call(MCP_SERVER_PAPERS_SEARCH, "echo", {"text": "stdio"}, _ctx())
        assert out.data["result"]["content"][0]["text"] == "stdio"
    finally:
        await router.aclose()
