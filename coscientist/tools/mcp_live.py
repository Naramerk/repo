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
)
from coscientist.mcp.errors import McpConfigError
from coscientist.mcp.runtime import AsyncSleep, AsyncWaitFor, Clock
from coscientist.mcp.transport import FileFetcher, HttpTransport
from coscientist.tools.mcp_adapter import McpToolProfile
from coscientist.tools.router import (
    MCP_SERVER_PAPER_ANALYSIS,
    MCP_SERVER_PAPERS_SEARCH,
    McpServerRoute,
    ToolRouter,
    ToolRouterConfig,
    ToolRouterError,
)

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


class LiveHttpServer(BaseModel):
    """One streamable-HTTP MCP server (docker-compose service endpoint)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: Identifier
    url: str = Field(min_length=1)
    api_key_env: Identifier | None
    tools: dict[str, McpToolProfile] = Field(min_length=1)

    @model_validator(mode="after")
    def _url_and_tools(self) -> LiveHttpServer:
        if any(ch.isspace() for ch in self.url):
            raise ValueError(f"url must not contain whitespace: {self.url!r}")
        for tool_name in self.tools:
            if not tool_name or any(ch.isspace() for ch in tool_name):
                raise ValueError(f"invalid tool name: {tool_name!r}")
        return self


class LiveMcpBundleConfig(BaseModel):
    """Explicit HTTP servers + tool profiles for a live ToolRouter open."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    servers: tuple[LiveHttpServer, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> LiveMcpBundleConfig:
        names = [s.name for s in self.servers]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate LiveHttpServer.name: {names}")
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
    return tuple(
        StreamableHttpServerConfig(
            transport="streamable_http",
            name=s.name,
            url=s.url,
            api_key_env=s.api_key_env,
        )
        for s in bundle.servers
    )


def router_config(bundle: LiveMcpBundleConfig) -> ToolRouterConfig:
    return ToolRouterConfig(
        servers=tuple(McpServerRoute(server_name=s.name, tools=dict(s.tools)) for s in bundle.servers)
    )


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
    ps, pa = dict(papers_search_tools), dict(paper_analysis_tools)
    if set(ps) != PAPERS_SEARCH_TOOLS:
        raise McpLiveError(f"papers-search tools must be exactly {sorted(PAPERS_SEARCH_TOOLS)}; got {sorted(ps)}")
    if set(pa) != PAPER_ANALYSIS_TOOLS:
        raise McpLiveError(f"paper-analysis tools must be exactly {sorted(PAPER_ANALYSIS_TOOLS)}; got {sorted(pa)}")
    return LiveMcpBundleConfig(
        servers=(
            LiveHttpServer(
                name=MCP_SERVER_PAPERS_SEARCH,
                url=papers_search_url,
                api_key_env=papers_search_api_key_env,
                tools=ps,
            ),
            LiveHttpServer(
                name=MCP_SERVER_PAPER_ANALYSIS,
                url=paper_analysis_url,
                api_key_env=paper_analysis_api_key_env,
                tools=pa,
            ),
        )
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
    file_fetcher: FileFetcher | None = None,
    http_transport: HttpTransport | None = None,
) -> tuple[McpClient, ToolRouter]:
    """Build client, ``ToolRouter.open``; caller owns ``router.aclose``."""
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
    await router.open(client)
    return client, router
