"""mcp_live: literature config + FakeStdio / real-stdio ToolRouter open."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.core.execution.domain import HitlPolicy
from coscientist.mcp.config import StdioServerConfig, StreamableHttpServerConfig
from coscientist.tools.contract import CallCtx
from coscientist.tools.mcp_adapter import McpToolProfile
from coscientist.tools.mcp_live import (
    MCP_SERVER_PAPER_ANALYSIS,
    MCP_SERVER_PAPERS_SEARCH,
    PAPER_ANALYSIS_TOOLS,
    PAPERS_SEARCH_TOOLS,
    LiveMcpBundleConfig,
    McpLiveError,
    McpToolRouterBundle,
    bundle_from_live,
    literature_live_bundle,
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
    async def fetch(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("no fetch")


def _ctx() -> CallCtx:
    return CallCtx(study_id="st", task_key="T0", attempt_id="att#1", hitl_policy=HitlPolicy())


def _p() -> McpToolProfile:
    return McpToolProfile(effect="read", is_fetch=False, idempotent=True, max_retries=0)


def _lit_tools() -> tuple[dict[str, McpToolProfile], dict[str, McpToolProfile]]:
    return {n: _p() for n in PAPERS_SEARCH_TOOLS}, {n: _p() for n in PAPER_ANALYSIS_TOOLS}


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
    live = literature_live_bundle(
        papers_search_url="http://127.0.0.1:7331/mcp",
        paper_analysis_url="http://127.0.0.1:7334/mcp",
        papers_search_api_key_env=None,
        paper_analysis_api_key_env="PA_KEY",
        papers_search_tools=ps,
        paper_analysis_tools=pa,
    )
    assert [(s.name, s.url, s.api_key_env) for s in live.http_servers] == [
        (MCP_SERVER_PAPERS_SEARCH, "http://127.0.0.1:7331/mcp", None),
        (MCP_SERVER_PAPER_ANALYSIS, "http://127.0.0.1:7334/mcp", "PA_KEY"),
    ]


def test_literature_refuses_wrong_papers_search_tools() -> None:
    ps, pa = _lit_tools()
    bad = dict(ps)
    del bad["search_entity"]
    with pytest.raises(McpLiveError, match="papers-search"):
        literature_live_bundle(
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


def test_route_invalid_tool_name_refused() -> None:
    with pytest.raises(ValidationError, match="tool name"):
        McpServerRoute(server_name="x", tools={"bad name": _p()})


def test_live_bundle_duplicate_http_name_refused() -> None:
    srv = _http(MCP_SERVER_PAPERS_SEARCH, "http://127.0.0.1:7331/mcp")
    route = McpServerRoute(server_name=MCP_SERVER_PAPERS_SEARCH, tools={"echo": _p()})
    with pytest.raises(ValidationError, match="unique"):
        LiveMcpBundleConfig(http_servers=(srv, srv), routes=(route, route))


def test_live_bundle_http_routes_name_mismatch_refused() -> None:
    with pytest.raises(ValidationError, match="diverge"):
        LiveMcpBundleConfig(
            http_servers=(_http(MCP_SERVER_PAPERS_SEARCH, "http://127.0.0.1:7331/mcp"),),
            routes=(McpServerRoute(server_name=MCP_SERVER_PAPER_ANALYSIS, tools={"echo": _p()}),),
        )


def test_bundle_from_live_replaces_servers() -> None:
    ps, pa = _lit_tools()
    live = literature_live_bundle(
        papers_search_url="http://127.0.0.1:7331/mcp",
        paper_analysis_url="http://127.0.0.1:7334/mcp",
        papers_search_api_key_env=None,
        paper_analysis_api_key_env=None,
        papers_search_tools=ps,
        paper_analysis_tools=pa,
    )
    base = make_config(
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
    b = bundle_from_live(base, live)
    assert [s.name for s in b.client.servers] == [
        MCP_SERVER_PAPERS_SEARCH,
        MCP_SERVER_PAPER_ANALYSIS,
    ]


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
