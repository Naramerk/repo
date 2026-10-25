"""mcp_live: literature config + FakeStdio / real-stdio ToolRouter open."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from coscientist.core.execution.domain import HitlPolicy
from coscientist.mcp.config import McpClientConfig, StdioServerConfig, StreamableHttpServerConfig
from coscientist.mcp.session import McpSession
from coscientist.tools.contract import CallCtx
from coscientist.tools.mcp_adapter import McpToolProfile
from coscientist.tools.mcp_live import (
    MCP_SERVER_PAPER_ANALYSIS,
    MCP_SERVER_PAPERS_SEARCH,
    PAPER_ANALYSIS_TOOLS,
    PAPERS_SEARCH_TOOLS,
    McpLiveError,
    McpToolRouterBundle,
    literature_live_bundle,
    load_mcp_tool_router_bundle,
    open_tool_router,
)
from coscientist.tools.router import (
    McpServerRoute,
    ToolRouter,
    ToolRouterConfig,
    ToolRouterError,
)
from tests.mcp.conftest import SystemClock, make_client, make_config, system_sleep, system_wait_for
from tests.mcp.fake_server import FakeStdioServer, default_handler

ECHO_STDIO = Path(__file__).resolve().parents[1] / "mcp" / "echo_stdio_server.py"


class _NoFetch:
    async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
        raise AssertionError("no fetch")


def _ctx() -> CallCtx:
    return CallCtx(study_id="st", task_key="T0", attempt_id="att#1", hitl_policy=HitlPolicy())


def _p() -> McpToolProfile:
    return McpToolProfile(effect="read", is_fetch=False, idempotent=True, max_retries=0)


def _lit_tools() -> tuple[dict[str, McpToolProfile], dict[str, McpToolProfile]]:
    return {n: _p() for n in PAPERS_SEARCH_TOOLS}, {n: _p() for n in PAPER_ANALYSIS_TOOLS}


def _base() -> McpClientConfig:
    return make_config(
        servers=(
            StdioServerConfig(
                transport="stdio",
                name="ph",
                command=sys.executable,
                args=("-c", "pass"),
                env_keys=(),
            ),
        )
    )


def _bundle(cmd: tuple[str, ...] = ("-c", "pass")) -> McpToolRouterBundle:
    return McpToolRouterBundle(
        client=make_config(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name=MCP_SERVER_PAPERS_SEARCH,
                    command=sys.executable,
                    args=cmd,
                    env_keys=(),
                ),
            )
        ),
        router=ToolRouterConfig(servers=(McpServerRoute(server_name=MCP_SERVER_PAPERS_SEARCH, tools={"echo": _p()}),)),
    )


def _http(name: str, url: str) -> StreamableHttpServerConfig:
    return StreamableHttpServerConfig(
        transport="streamable_http",
        name=name,
        url=url,
        api_key_env=None,
    )


def test_literature_live_bundle_urls() -> None:
    ps, pa = _lit_tools()
    bundle = literature_live_bundle(
        _base(),
        papers_search_url="http://127.0.0.1:7331/mcp",
        paper_analysis_url="http://127.0.0.1:7334/mcp",
        papers_search_api_key_env=None,
        paper_analysis_api_key_env="PA_KEY",
        papers_search_tools=ps,
        paper_analysis_tools=pa,
    )
    servers = bundle.client.servers
    assert [type(s) for s in servers] == [StreamableHttpServerConfig, StreamableHttpServerConfig]
    assert [(s.name, s.url, s.api_key_env) for s in servers] == [
        (MCP_SERVER_PAPERS_SEARCH, "http://127.0.0.1:7331/mcp", None),
        (MCP_SERVER_PAPER_ANALYSIS, "http://127.0.0.1:7334/mcp", "PA_KEY"),
    ]
    assert [r.server_name for r in bundle.router.servers] == [
        MCP_SERVER_PAPERS_SEARCH,
        MCP_SERVER_PAPER_ANALYSIS,
    ]


def test_literature_refuses_wrong_papers_search_tools() -> None:
    ps, pa = _lit_tools()
    bad = dict(ps)
    del bad["search_entity"]
    with pytest.raises(McpLiveError, match="papers-search"):
        literature_live_bundle(
            _base(),
            papers_search_url="http://127.0.0.1:7331/mcp",
            paper_analysis_url="http://127.0.0.1:7334/mcp",
            papers_search_api_key_env=None,
            paper_analysis_api_key_env=None,
            papers_search_tools=bad,
            paper_analysis_tools=pa,
        )


def test_literature_refuses_wrong_paper_analysis_tools() -> None:
    ps, pa = _lit_tools()
    bad = dict(pa)
    del bad["explore_my_papers"]
    with pytest.raises(McpLiveError, match="paper-analysis"):
        literature_live_bundle(
            _base(),
            papers_search_url="http://127.0.0.1:7331/mcp",
            paper_analysis_url="http://127.0.0.1:7334/mcp",
            papers_search_api_key_env=None,
            paper_analysis_api_key_env=None,
            papers_search_tools=ps,
            paper_analysis_tools=bad,
        )


def test_bundle_names_must_align() -> None:
    with pytest.raises(ValidationError, match="diverge"):
        McpToolRouterBundle(
            client=make_config(
                servers=(
                    StdioServerConfig(
                        transport="stdio",
                        name="a",
                        command=sys.executable,
                        args=("-c", "pass"),
                        env_keys=(),
                    ),
                )
            ),
            router=ToolRouterConfig(servers=(McpServerRoute(server_name="b", tools={"echo": _p()}),)),
        )


def test_streamable_http_url_whitespace_refused() -> None:
    with pytest.raises(ValidationError, match="whitespace"):
        _http("x", "http://x ")


def test_load_bundle_from_json_text_accepts_arrays() -> None:
    """JSON bundles (CLI file path) carry arrays; strict tuples need the JSON parser."""
    text = json.dumps(
        {
            "client": {
                **_base().model_dump(),
                "servers": [
                    {
                        "transport": "stdio",
                        "name": "srv",
                        "command": sys.executable,
                        "args": ["-c", "pass"],
                        "env_keys": [],
                    }
                ],
            },
            "router": {
                "servers": [
                    {
                        "server_name": "srv",
                        "tools": {"echo": {"effect": "read", "is_fetch": False, "idempotent": True, "max_retries": 0}},
                    }
                ]
            },
        }
    )
    bundle = load_mcp_tool_router_bundle(text)
    assert bundle.client.servers[0].name == "srv"
    assert bundle.router.servers[0].server_name == "srv"
    # Decoded mapping with lists is still refused for tuples (strict contract unchanged).
    with pytest.raises(ValidationError, match="tuple"):
        load_mcp_tool_router_bundle(json.loads(text))


def test_route_invalid_tool_name_refused() -> None:
    with pytest.raises(ValidationError, match="tool name"):
        McpServerRoute(server_name="x", tools={"bad name": _p()})


def test_client_duplicate_http_name_refused() -> None:
    srv = _http(MCP_SERVER_PAPERS_SEARCH, "http://127.0.0.1:7331/mcp")
    with pytest.raises(ValidationError, match="unique"):
        make_config(servers=(srv, srv))


@pytest.mark.asyncio
async def test_fake_stdio_attach_integration() -> None:
    client = make_client(_bundle().client, file_fetcher=_NoFetch())
    router = ToolRouter(_bundle().router)
    async with FakeStdioServer(default_handler) as server:
        session = await client.connect_memory(MCP_SERVER_PAPERS_SEARCH, server.client)
        try:
            await router.attach_session(session, server_name=MCP_SERVER_PAPERS_SEARCH)
            out = await router.call(MCP_SERVER_PAPERS_SEARCH, "echo", {"text": "live"}, _ctx())
            assert out.data["result"]["content"][0]["text"] == "live"
        finally:
            await router.aclose()


@pytest.mark.asyncio
async def test_open_tool_router_stdio_echo_file() -> None:
    _c, router = await open_tool_router(
        _bundle(("-u", str(ECHO_STDIO))),
        clock=SystemClock(),
        sleep=system_sleep(),
        wait_for=system_wait_for(),
        file_fetcher=_NoFetch(),
        http_transport=None,
    )
    try:
        out = await router.call(MCP_SERVER_PAPERS_SEARCH, "echo", {"text": "open"}, _ctx())
        assert out.data["result"]["content"][0]["text"] == "open"
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_open_tool_router_requires_fetcher_or_http() -> None:
    with pytest.raises(ToolRouterError, match="failed to build McpClient"):
        await open_tool_router(
            _bundle(),
            clock=SystemClock(),
            sleep=system_sleep(),
            wait_for=system_wait_for(),
            file_fetcher=None,
            http_transport=None,
        )


@pytest.mark.asyncio
async def test_open_tool_router_bind_failure_closes_session() -> None:
    # Echo stdio lists ``echo``; extra binding → unknown-tools refuse after connect.
    # Session aclose is owned by bind_mcp_server_tools / ToolRouter.open cleanup.
    bundle = McpToolRouterBundle(
        client=make_config(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name=MCP_SERVER_PAPERS_SEARCH,
                    command=sys.executable,
                    args=("-u", str(ECHO_STDIO)),
                    env_keys=(),
                ),
            )
        ),
        router=ToolRouterConfig(
            servers=(
                McpServerRoute(
                    server_name=MCP_SERVER_PAPERS_SEARCH,
                    tools={"echo": _p(), "missing-tool": _p()},
                ),
            )
        ),
    )
    closed: list[McpSession] = []
    original_aclose = McpSession.aclose

    async def _track_aclose(self: McpSession) -> None:
        closed.append(self)
        await original_aclose(self)

    expected = "failed to bind server 'papers-search': McpToolProfile for unknown tools: ['missing-tool']"
    with patch.object(McpSession, "aclose", autospec=True, side_effect=_track_aclose):
        with pytest.raises(ToolRouterError) as ei:
            await open_tool_router(
                bundle,
                clock=SystemClock(),
                sleep=system_sleep(),
                wait_for=system_wait_for(),
                file_fetcher=_NoFetch(),
                http_transport=None,
            )
    assert str(ei.value) == expected
    assert len(closed) == 1
    assert closed[0].server_name == MCP_SERVER_PAPERS_SEARCH
