"""ToolRouter(MCP): route calls via mcp_adapter (fake stdio + real stdio)."""

from __future__ import annotations

import inspect
import re
import sys
import textwrap
from collections.abc import Mapping
from typing import Any
from unittest.mock import create_autospec, patch

import pytest
from pydantic import ValidationError

from coscientist.core.execution.domain import HitlPolicy
from coscientist.mcp.client import McpClient
from coscientist.mcp.config import StdioServerConfig
from coscientist.mcp.session import McpSession
from coscientist.tools.contract import CallCtx
from coscientist.tools.mcp_adapter import (
    McpAdapterError,
    McpBoundTool,
    McpToolKey,
    McpToolProfile,
)
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
    async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
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


def _stdio_client(*names: str) -> McpClient:
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
async def test_router_attach_unknown_server_fail_closed() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_A)))
    client = _stdio_client(SERVER_A)
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(SERVER_A, server.client)
        try:
            with pytest.raises(ToolRouterError, match="unknown server"):
                await router.attach_session(session, server_name=SERVER_B)
        finally:
            await session.aclose()


@pytest.mark.asyncio
async def test_router_require_tool_refuses_when_not_open() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_A)))
    with pytest.raises(ToolRouterError, match="not open"):
        router.require_tool(SERVER_A, "echo")


def test_mark_open_empty_servers_raises() -> None:
    cfg = ToolRouterConfig.model_construct(servers=())
    router = ToolRouter(cfg)
    with pytest.raises(ToolRouterError, match="cannot mark_open"):
        router.mark_open()


def test_mark_open_clears_not_open_gate() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_A)))
    with pytest.raises(ToolRouterError, match="not open"):
        router.require_tool(SERVER_A, "echo")
    router.mark_open()
    with pytest.raises(ToolRouterError, match="unknown tool"):
        router.require_tool(SERVER_A, "echo")


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
    tool = create_autospec(McpBoundTool, instance=True)
    session = create_autospec(McpSession, instance=True)

    async def _bind(
        _client: McpClient,
        server_name: str,
        *,
        bindings: Mapping[str, McpToolProfile],
    ) -> tuple[McpSession, dict[McpToolKey, McpBoundTool]]:
        del _client, server_name, bindings
        # Colliding key across servers → fail-closed duplicate.
        return session, {("dup", "echo"): tool}

    with patch("coscientist.tools.router.bind_mcp_server_tools", side_effect=_bind):
        with pytest.raises(ToolRouterError, match="duplicate MCP tool key"):
            await router.open(client)
    session.aclose.assert_awaited()


@pytest.mark.asyncio
async def test_attach_session_bind_failure() -> None:
    # echo is listed on the fake server; extra binding → mcp_adapter.py unknown-tools path.
    router = ToolRouter(_router_cfg(_route(SERVER_A, tools={"echo": _profile(), "missing-tool": _profile()})))
    client = _stdio_client(SERVER_A)
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(SERVER_A, server.client)
        try:
            with pytest.raises(ToolRouterError, match="McpToolProfile for unknown tools") as ei:
                await router.attach_session(session, server_name=SERVER_A)
            assert isinstance(ei.value.__cause__, McpAdapterError)
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

        with (
            patch.object(sess1, "aclose", side_effect=_boom),
            patch.object(sess2, "aclose", side_effect=_ok),
        ):
            with pytest.raises(ToolRouterCloseError, match="session close failed \\(1\\)") as ei:
                await router.aclose()
        assert closed == ["first", "second"]
        assert isinstance(ei.value.__cause__, OSError)
        assert len(ei.value.causes) == 1
        assert isinstance(ei.value.causes[0], OSError)
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

        with (
            patch.object(sess1, "aclose", side_effect=_boom1),
            patch.object(sess2, "aclose", side_effect=_boom2),
        ):
            with pytest.raises(ToolRouterCloseError, match="session close failed \\(2\\)") as ei:
                await router.aclose()
        err = ei.value
        assert len(err.causes) == 2
        assert isinstance(err.causes[0], OSError)
        assert str(err.causes[0]) == "close-a"
        assert isinstance(err.causes[1], TimeoutError)
        assert str(err.causes[1]) == "close-b"
        assert str(err) == "session close failed (2): OSError('close-a'); TimeoutError('close-b')"
        assert isinstance(err.__cause__, ExceptionGroup)
        assert [type(e) for e in err.__cause__.exceptions] == [OSError, TimeoutError]


def test_tool_router_close_error_causes_is_required_keyword() -> None:
    causes = inspect.signature(ToolRouterCloseError.__init__).parameters["causes"]
    assert causes.kind is inspect.Parameter.KEYWORD_ONLY
    assert causes.default is inspect.Parameter.empty


def test_tool_router_close_error_rejects_empty_causes() -> None:
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
    first_session = create_autospec(McpSession, instance=True)

    async def _bind(
        _client: McpClient,
        server_name: str,
        *,
        bindings: Mapping[str, McpToolProfile],
    ) -> tuple[McpSession, dict[McpToolKey, McpBoundTool]]:
        del _client, bindings
        if server_name == SERVER_A:
            tool = create_autospec(McpBoundTool, instance=True)
            return first_session, {(SERVER_A, "echo"): tool}
        raise exc

    with patch("coscientist.tools.router.bind_mcp_server_tools", side_effect=_bind):
        with pytest.raises(type(exc), match=re.escape(str(exc))):
            await router.open(client)
    first_session.aclose.assert_awaited_once()
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


@pytest.mark.asyncio
async def test_resolve_not_open_raises() -> None:
    from coscientist.tools.router import ToolNeed

    router = ToolRouter(_router_cfg(_route(SERVER_A)))
    with pytest.raises(ToolRouterError, match="not open"):
        router.resolve(ToolNeed(kind="by_name", value=f"{SERVER_A}:echo"))


@pytest.mark.asyncio
async def test_resolve_by_name_tool_found_and_missing() -> None:
    from coscientist.tools.router import ToolNeed

    cfg = _router_cfg(_route(SERVER_A))
    router = ToolRouter(cfg)
    client = _stdio_client(SERVER_A)
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(SERVER_A, server.client)
        try:
            await router.attach_session(session, server_name=SERVER_A)
            bare = router.resolve(ToolNeed(kind="by_name", value="echo"))
            assert bare.status == "tool_missing"
            assert bare.server_name is None
            assert bare.tool_name is None
            found = router.resolve(ToolNeed(kind="by_name", value=f"{SERVER_A}:echo"))
            assert found.status == "tool_found"
            assert found.server_name == SERVER_A
            assert found.tool_name == "echo"
            missing = router.resolve(ToolNeed(kind="by_name", value="nope"))
            assert missing.status == "tool_missing"
            assert missing.server_name is None
            assert missing.tool_name is None
            query = router.resolve(ToolNeed(kind="by_query", value="echo"))
            assert query.status == "tool_missing"
        finally:
            await router.aclose()


@pytest.mark.asyncio
async def test_resolve_ambiguous_bare_name_is_missing() -> None:
    from coscientist.tools.router import ToolNeed

    cfg = _router_cfg(
        _route(SERVER_A, tools={"echo": _profile()}),
        _route(SERVER_B, tools={"echo": _profile()}),
    )
    router = ToolRouter(cfg)
    client = _stdio_client(SERVER_A, SERVER_B)
    async with FakeStdioServer(default_handler) as s1, FakeStdioServer(default_handler) as s2:
        sess1 = await client.connect_memory(SERVER_A, s1.client)
        sess2 = await client.connect_memory(SERVER_B, s2.client)
        try:
            await router.attach_session(sess1, server_name=SERVER_A)
            await router.attach_session(sess2, server_name=SERVER_B)
            amb = router.resolve(ToolNeed(kind="by_name", value="echo"))
            assert amb.status == "tool_missing"
            ok = router.resolve(ToolNeed(kind="by_name", value=f"{SERVER_A}:echo"))
            assert ok.status == "tool_found"
            assert ok.server_name == SERVER_A
        finally:
            await router.aclose()


def test_resolution_tool_found_requires_server_name() -> None:
    from coscientist.tools.router import Resolution

    with pytest.raises(ValidationError, match="tool_found requires non-empty server_name"):
        Resolution(status="tool_found", server_name=None, tool_name="echo")


def test_resolution_tool_found_requires_tool_name() -> None:
    from coscientist.tools.router import Resolution

    with pytest.raises(ValidationError, match="tool_found requires non-empty tool_name"):
        Resolution(status="tool_found", server_name=SERVER_A, tool_name=None)


def test_resolution_non_found_forbids_server_name() -> None:
    from coscientist.tools.router import Resolution

    with pytest.raises(ValidationError, match="tool_missing requires server_name=tool_name=None"):
        Resolution(status="tool_missing", server_name=SERVER_A, tool_name=None)


@pytest.mark.asyncio
async def test_resolve_coder_with_attached_route() -> None:
    from coscientist.tools.router import ToolNeed

    router = ToolRouter(_router_cfg(_route(SERVER_A)))
    client = _stdio_client(SERVER_A)
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(SERVER_A, server.client)
        try:
            await router.attach_session(session, server_name=SERVER_A)
            # Without an attached coder route the coder need is tool_missing
            # (typed refuse — no silent platform guess).
            before = router.resolve(ToolNeed(kind="by_name", value="coder"))
            assert before.status == "tool_missing"
            assert before.server_name is None
            assert before.tool_name is None
            router.attach_coder_route("local-python")
            coder = router.resolve(ToolNeed(kind="by_name", value="coder"))
            assert coder.status == "coder"
            assert coder.server_name is None
            assert coder.tool_name is None
            # Other needs are unaffected by the coder route.
            found = router.resolve(ToolNeed(kind="by_name", value=f"{SERVER_A}:echo"))
            assert found.status == "tool_found"
            still_missing = router.resolve(ToolNeed(kind="by_query", value="coder"))
            assert still_missing.status == "tool_missing"
        finally:
            await router.aclose()


def test_attach_coder_route_double_attach_refuses() -> None:
    router = ToolRouter(_router_cfg(_route(SERVER_A)))
    router.mark_open()
    router.attach_coder_route("bwrap")
    with pytest.raises(ToolRouterError, match="already has a coder route"):
        router.attach_coder_route("local-python")


def test_attach_coder_route_invalid_backend_refuses() -> None:
    from coscientist.tools.contract import ToolError

    router = ToolRouter(_router_cfg(_route(SERVER_A)))
    router.mark_open()
    with pytest.raises(ToolError, match="coder backend refuse"):
        router.attach_coder_route("docker")  # type: ignore[arg-type]


def test_coder_need_value_matches_coder_tool_and_derive_hint() -> None:
    """Single name for the coder binding across router / coder tool / derive."""
    from coscientist.agents.derive import CODER_HINT
    from coscientist.tools.coder import CODER_TOOL
    from coscientist.tools.router import CODER_NEED_VALUE

    assert CODER_NEED_VALUE == CODER_TOOL.spec.name == CODER_HINT
