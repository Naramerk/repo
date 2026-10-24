"""mcp_live: literature config + FakeStdio / real-stdio ToolRouter open."""

from __future__ import annotations

import sys
from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.core.execution.domain import HitlPolicy
from coscientist.mcp.config import StdioServerConfig
from coscientist.tools.contract import CallCtx
from coscientist.tools.mcp_adapter import McpToolProfile
from coscientist.tools.mcp_live import (
    PAPER_ANALYSIS_TOOLS,
    PAPERS_SEARCH_TOOLS,
    LiveHttpServer,
    LiveMcpBundleConfig,
    McpLiveError,
    McpToolRouterBundle,
    bundle_from_live,
    literature_live_bundle,
    open_tool_router,
    streamable_servers,
)
from coscientist.tools.router import (
    MCP_SERVER_PAPER_ANALYSIS,
    MCP_SERVER_PAPERS_SEARCH,
    McpServerRoute,
    ToolRouter,
    ToolRouterConfig,
    ToolRouterError,
)
from tests.mcp.conftest import SystemClock, make_client, make_config, system_sleep, system_wait_for
from tests.mcp.fake_server import FakeStdioServer, default_handler

_ECHO = (
    "import json,sys\n"
    "def R(q,r):\n"
    " sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':r})+'\\n');sys.stdout.flush()\n"
    "while True:\n"
    " q=json.loads(sys.stdin.readline() or 'null') or sys.exit(0); m=q.get('method')\n"
    " if m=='initialize': R(q,{'protocolVersion':q['params']['protocolVersion'],"
    "'capabilities':{'tools':{}},'serverInfo':{'name':'x','version':'0'}})\n"
    " elif m=='notifications/initialized': pass\n"
    " elif m=='tools/list': R(q,{'tools':[{'name':'echo','description':'e',"
    "'inputSchema':{'type':'object'}}]})\n"
    " elif m=='tools/call': R(q,{'content':[{'type':'text',"
    "'text':str(q['params']['arguments']['text'])}],'isError':False})\n"
    " else: raise SystemExit(m)\n"
)


class _NoFetch:
    async def fetch(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("no fetch")


def _ctx() -> CallCtx:
    return CallCtx(study_id="st", task_key="T0", attempt_id="att#1", hitl_policy=HitlPolicy())


def _p() -> McpToolProfile:
    return McpToolProfile(effect="read", is_fetch=False, idempotent=True, max_retries=0)


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


def test_literature_and_bundle_guards() -> None:
    ps = {n: _p() for n in PAPERS_SEARCH_TOOLS}
    pa = {n: _p() for n in PAPER_ANALYSIS_TOOLS}
    live = literature_live_bundle(
        papers_search_url="http://127.0.0.1:7331/mcp",
        paper_analysis_url="http://127.0.0.1:7334/mcp",
        papers_search_api_key_env=None,
        paper_analysis_api_key_env="PA_KEY",
        papers_search_tools=ps,
        paper_analysis_tools=pa,
    )
    assert [(s.name, s.url) for s in streamable_servers(live)] == [
        (MCP_SERVER_PAPERS_SEARCH, "http://127.0.0.1:7331/mcp"),
        (MCP_SERVER_PAPER_ANALYSIS, "http://127.0.0.1:7334/mcp"),
    ]
    bad_ps = dict(ps)
    del bad_ps["search_entity"]
    with pytest.raises(McpLiveError, match="papers-search"):
        literature_live_bundle(
            papers_search_url="http://127.0.0.1:7331/mcp",
            paper_analysis_url="http://127.0.0.1:7334/mcp",
            papers_search_api_key_env=None,
            paper_analysis_api_key_env=None,
            papers_search_tools=bad_ps,
            paper_analysis_tools=pa,
        )
    bad_pa = dict(pa)
    del bad_pa["explore_my_papers"]
    with pytest.raises(McpLiveError, match="paper-analysis"):
        literature_live_bundle(
            papers_search_url="http://127.0.0.1:7331/mcp",
            paper_analysis_url="http://127.0.0.1:7334/mcp",
            papers_search_api_key_env=None,
            paper_analysis_api_key_env=None,
            papers_search_tools=ps,
            paper_analysis_tools=bad_pa,
        )
    with pytest.raises(ValidationError, match="diverge"):
        McpToolRouterBundle(
            client=make_config(
                servers=(
                    StdioServerConfig(
                        transport="stdio", name="a", command=sys.executable, args=("-c", "pass"), env_keys=()
                    ),
                )
            ),
            router=ToolRouterConfig(servers=(McpServerRoute(server_name="b", tools={"echo": _p()}),)),
        )
    with pytest.raises(ValidationError, match="whitespace"):
        LiveHttpServer(name="x", url="http://x ", api_key_env=None, tools={"t": _p()})
    with pytest.raises(ValidationError, match="invalid tool name"):
        LiveHttpServer(name="x", url="http://127.0.0.1:7331/mcp", api_key_env=None, tools={"bad name": _p()})
    dup = LiveHttpServer(
        name=MCP_SERVER_PAPERS_SEARCH,
        url="http://127.0.0.1:7331/mcp",
        api_key_env=None,
        tools={"echo": _p()},
    )
    with pytest.raises(ValidationError, match="duplicate LiveHttpServer.name"):
        LiveMcpBundleConfig(servers=(dup, dup))
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
    assert [s.name for s in b.client.servers] == [MCP_SERVER_PAPERS_SEARCH, MCP_SERVER_PAPER_ANALYSIS]


@pytest.mark.asyncio
async def test_fake_stdio_and_open_tool_router() -> None:
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
    _c, router = await open_tool_router(
        _bundle(("-u", "-c", _ECHO)),
        clock=SystemClock(),
        sleep=system_sleep(),
        wait_for=system_wait_for(),
        file_fetcher=_NoFetch(),
    )
    try:
        out = await router.call(MCP_SERVER_PAPERS_SEARCH, "echo", {"text": "open"}, _ctx())
        assert out.data["result"]["content"][0]["text"] == "open"
    finally:
        await router.aclose()
    with pytest.raises(ToolRouterError, match="failed to build McpClient"):
        await open_tool_router(_bundle(), clock=SystemClock(), sleep=system_sleep(), wait_for=system_wait_for())
