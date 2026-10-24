"""M1 live MCP: ``McpClientConfig`` + ``ToolRouter`` for legacy literature servers.

Launch: ``mcp-servers/docker-compose.yml`` (build context ``COSCIENTIST_DEV_ROOT`` →
aimclub/CoResearchGraph ``mcp-servers/``). URLs/tools are required — no defaults.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coscientist.mcp.client import McpClient
from coscientist.mcp.config import (
    Identifier,
    McpClientConfig,
    McpServers,
    StreamableHttpServerConfig,
    unique_mcp_server_names,
)
from coscientist.mcp.errors import McpConfigError
from coscientist.mcp.runtime import AsyncSleep, AsyncWaitFor, Clock
from coscientist.mcp.transport import FileFetcher, HttpTransport
from coscientist.tools.mcp_adapter import McpToolProfile
from coscientist.tools.router import (
    McpServerRoute,
    ToolRouter,
    ToolRouterConfig,
    ToolRouterError,
)

# Legacy literature server ids (compose / CAP #3); not ToolRouter defaults.
MCP_SERVER_PAPERS_SEARCH = "papers-search"
MCP_SERVER_PAPER_ANALYSIS = "paper-analysis"

# Exact tool names from legacy mcp-servers/* (fail-closed).
PAPERS_SEARCH_TOOLS = frozenset({"search_entity", "search_papers", "download_papers_from_search"})
PAPER_ANALYSIS_TOOLS = frozenset(
    {
        "explore_scientific_database",
        "explore_my_papers",
        "find_papers_in_db",
        "find_relevant_data_in_db",
    }
)


class McpLiveError(Exception):
    """Fail-closed live-bundle / literature-endpoint error."""


class LiveMcpBundleConfig(BaseModel):
    """Paired ``StreamableHttpServerConfig`` + ``McpServerRoute`` (same names, same order)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    http_servers: tuple[StreamableHttpServerConfig, ...] = Field(min_length=1)
    routes: tuple[McpServerRoute, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _align(self) -> LiveMcpBundleConfig:
        unique_mcp_server_names(self.http_servers)
        ToolRouterConfig(servers=self.routes)  # unique server_name + tool ids
        http_names = [s.name for s in self.http_servers]
        route_names = [r.server_name for r in self.routes]
        if http_names != route_names:
            raise ValueError(f"http_servers/routes names diverge: http={http_names} routes={route_names}")
        return self


class McpToolRouterBundle(BaseModel):
    """Client server names must equal router server names."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    client: McpClientConfig
    router: ToolRouterConfig

    @model_validator(mode="after")
    def _align_names(self) -> McpToolRouterBundle:
        client_names = {s.name for s in self.client.servers}
        router_names = {s.server_name for s in self.router.servers}
        if client_names != router_names:
            raise ValueError(
                f"client/router server names diverge: client={sorted(client_names)} router={sorted(router_names)}"
            )
        return self


def streamable_servers(bundle: LiveMcpBundleConfig) -> McpServers:
    return bundle.http_servers


def router_config(bundle: LiveMcpBundleConfig) -> ToolRouterConfig:
    return ToolRouterConfig(servers=bundle.routes)


def _require_exact_tools(
    label: str,
    tools: Mapping[str, McpToolProfile],
    expected: frozenset[str],
) -> dict[str, McpToolProfile]:
    got = dict(tools)
    if set(got) != expected:
        raise McpLiveError(f"{label} tools must be exactly {sorted(expected)}; got {sorted(got)}")
    return got


def literature_live_bundle(
    *,
    papers_search_url: str,
    paper_analysis_url: str,
    papers_search_api_key_env: Identifier | None,
    paper_analysis_api_key_env: Identifier | None,
    papers_search_tools: Mapping[str, McpToolProfile],
    paper_analysis_tools: Mapping[str, McpToolProfile],
) -> LiveMcpBundleConfig:
    """``papers-search`` + ``paper-analysis`` with exact legacy tool sets."""
    ps = _require_exact_tools("papers-search", papers_search_tools, PAPERS_SEARCH_TOOLS)
    pa = _require_exact_tools("paper-analysis", paper_analysis_tools, PAPER_ANALYSIS_TOOLS)
    return LiveMcpBundleConfig(
        http_servers=(
            StreamableHttpServerConfig(
                transport="streamable_http",
                name=MCP_SERVER_PAPERS_SEARCH,
                url=papers_search_url,
                api_key_env=papers_search_api_key_env,
            ),
            StreamableHttpServerConfig(
                transport="streamable_http",
                name=MCP_SERVER_PAPER_ANALYSIS,
                url=paper_analysis_url,
                api_key_env=paper_analysis_api_key_env,
            ),
        ),
        routes=(
            McpServerRoute(server_name=MCP_SERVER_PAPERS_SEARCH, tools=ps),
            McpServerRoute(server_name=MCP_SERVER_PAPER_ANALYSIS, tools=pa),
        ),
    )


def bundle_from_live(base_client: McpClientConfig, live: LiveMcpBundleConfig) -> McpToolRouterBundle:
    """Compose client+router; ``base_client`` supplies timeouts (servers replaced)."""
    return McpToolRouterBundle(
        client=base_client.model_copy(update={"servers": streamable_servers(live)}),
        router=router_config(live),
    )


async def open_tool_router(
    bundle: McpToolRouterBundle,
    *,
    clock: Clock,
    sleep: AsyncSleep,
    wait_for: AsyncWaitFor,
    file_fetcher: FileFetcher | None,
    http_transport: HttpTransport | None,
) -> tuple[McpClient, ToolRouter]:
    """Build client, ``ToolRouter.open``; caller owns ``router.aclose``.

    ``file_fetcher`` and ``http_transport`` are explicit (pass ``None`` when unused).
    """
    try:
        client = McpClient(
            bundle.client,
            clock=clock,
            sleep=sleep,
            wait_for=wait_for,
            file_fetcher=file_fetcher,
            http_transport=http_transport,
        )
    except McpConfigError as exc:
        raise ToolRouterError(f"failed to build McpClient: {exc}") from exc
    router = ToolRouter(bundle.router)
    try:
        await router.open(client)
    except (ToolRouterError, OSError, TimeoutError):
        await router.aclose()
        raise
    return client, router
