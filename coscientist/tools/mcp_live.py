"""M1 live MCP: ``McpClientConfig`` + ``ToolRouter`` for legacy literature servers.

Launch: ``mcp-servers/docker-compose.yml`` (build context ``COSCIENTIST_DEV_ROOT`` →
aimclub/CoResearchGraph ``mcp-servers/``). URLs/tools are required — no defaults.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, model_validator

from coscientist.mcp.client import McpClient
from coscientist.mcp.config import (
    Identifier,
    McpClientConfig,
    StreamableHttpServerConfig,
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

# Exact tool names from aimclub/CoResearchGraph (outside this repo; fail-closed):
# papers-search → mcp-servers/papers-search-mcp-server (CAP #3 «MCP papers_search»);
# paper-analysis → mcp-servers/paper-analysis-mcp-server/paper_analysis_server.py:18-32
# (docs/CAPABILITIES.md #3).
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
    base_client: McpClientConfig,
    *,
    papers_search_url: str,
    paper_analysis_url: str,
    papers_search_api_key_env: Identifier | None,
    paper_analysis_api_key_env: Identifier | None,
    papers_search_tools: Mapping[str, McpToolProfile],
    paper_analysis_tools: Mapping[str, McpToolProfile],
) -> McpToolRouterBundle:
    """``papers-search`` + ``paper-analysis``; timeouts from ``base_client`` (servers replaced)."""
    ps = _require_exact_tools("papers-search", papers_search_tools, PAPERS_SEARCH_TOOLS)
    pa = _require_exact_tools("paper-analysis", paper_analysis_tools, PAPER_ANALYSIS_TOOLS)
    servers = (
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
    )
    client = McpClientConfig.model_validate({**base_client.model_dump(), "servers": servers})
    return McpToolRouterBundle(
        client=client,
        router=ToolRouterConfig(
            servers=(
                McpServerRoute(server_name=MCP_SERVER_PAPERS_SEARCH, tools=ps),
                McpServerRoute(server_name=MCP_SERVER_PAPER_ANALYSIS, tools=pa),
            )
        ),
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
    Session cleanup on open failure is owned by ``ToolRouter.open``.
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
    await router.open(client)
    return client, router


class _RefuseFileFetch:
    """Stdio-only CLI path: file URI fetch is not configured (fail closed)."""

    async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
        del max_bytes, timeout_s
        raise McpLiveError(f"MCP file fetch not configured for CLI live path: {url!r}")


def load_mcp_tool_router_bundle(raw: object) -> McpToolRouterBundle:
    """Validate a JSON bundle (text) or decoded object as ``McpToolRouterBundle``.

    Strict tuple fields reject JSON arrays through ``model_validate`` on a decoded
    mapping, so JSON text is validated via pydantic's JSON parser (same strict
    model, no weakening). Exact servers/tools; no discovery.
    """
    if isinstance(raw, str):
        return McpToolRouterBundle.model_validate_json(raw)
    return McpToolRouterBundle.model_validate(raw)


async def open_tool_router_for_cli(
    bundle: McpToolRouterBundle,
    *,
    clock: Clock,
    sleep: AsyncSleep,
    wait_for: AsyncWaitFor,
) -> tuple[McpClient, ToolRouter]:
    """CLI live path: inject ``HttpxTransport`` when any HTTP server is configured.

    Stdio-only bundles get a refuse-closed file fetcher (no silent HTTP client).
    Caller owns ``router.aclose``.
    """
    needs_http = any(isinstance(s, StreamableHttpServerConfig) for s in bundle.client.servers)
    if needs_http:
        # Lazy import: keeps ``mcp`` extra optional for cassette-only CLI installs.
        from coscientist.mcp.httpx_transport import HttpxTransport

        return await open_tool_router(
            bundle,
            clock=clock,
            sleep=sleep,
            wait_for=wait_for,
            file_fetcher=None,
            http_transport=HttpxTransport(),
        )
    return await open_tool_router(
        bundle,
        clock=clock,
        sleep=sleep,
        wait_for=wait_for,
        file_fetcher=_RefuseFileFetch(),
        http_transport=None,
    )
