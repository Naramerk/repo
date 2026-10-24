"""McpClient: connect / list_tools / call_tool from an explicit server profile."""

from __future__ import annotations

from coscientist.mcp.config import (
    McpClientConfig,
    StdioServerConfig,
    StreamableHttpServerConfig,
)
from coscientist.mcp.errors import McpConfigError
from coscientist.mcp.files import SafeHttpFileFetcher
from coscientist.mcp.http_transport import StreamableHttpSession
from coscientist.mcp.session import McpSession
from coscientist.mcp.ssrf import HostnameResolver
from coscientist.mcp.stdio import StdioTransport
from coscientist.mcp.transport import FileFetcher, HttpTransport, MessageTransport


class McpClient:
    """Thin MCP client. Servers come only from ``config.servers`` (no discovery)."""

    def __init__(
        self,
        config: McpClientConfig,
        *,
        file_fetcher: FileFetcher | None = None,
        http_transport: HttpTransport | None = None,
        resolver: HostnameResolver | None = None,
    ) -> None:
        self._config = config
        self._http_transport = http_transport
        if file_fetcher is not None:
            self._file_fetcher = file_fetcher
        else:
            if http_transport is None:
                raise McpConfigError("file_fetcher or http_transport is required to download MCP file URIs")
            self._file_fetcher = SafeHttpFileFetcher(
                http_transport,
                allowed_schemes=frozenset(config.fetch_uri_schemes),
                max_redirect_hops=config.max_redirect_hops,
                allowed_content_types=frozenset(config.allowed_file_content_types),
                allowed_content_encodings=frozenset(config.allowed_content_encodings),
                read_chunk_bytes=config.stdio_read_chunk_bytes,
                resolver=resolver,
            )

    async def connect(self, server_name: str) -> McpSession:
        server = self._lookup(server_name)
        if isinstance(server, StdioServerConfig):
            transport = await StdioTransport.start(
                command=server.command,
                args=server.args,
                env_keys=server.env_keys,
                terminate_grace_s=self._config.terminate_grace_s,
                kill_grace_s=self._config.kill_grace_s,
                read_chunk_bytes=self._config.stdio_read_chunk_bytes,
                max_stderr_bytes=self._config.max_stderr_bytes,
            )
            session = McpSession(
                config=self._config,
                server_name=server.name,
                file_fetcher=self._file_fetcher,
                stdio=transport,
            )
        else:
            assert isinstance(server, StreamableHttpServerConfig)
            if self._http_transport is None:
                raise McpConfigError("http_transport is required for streamable_http servers")
            http = StreamableHttpSession(
                url=server.url,
                api_key_env=server.api_key_env,
                http=self._http_transport,
                max_response_bytes=self._config.max_response_bytes,
                read_chunk_bytes=self._config.stdio_read_chunk_bytes,
                request_ok_statuses=frozenset(self._config.http_request_ok_statuses),
                notification_ok_statuses=frozenset(self._config.http_notification_ok_statuses),
                protocol_version=self._config.protocol_version,
            )
            session = McpSession(
                config=self._config,
                server_name=server.name,
                file_fetcher=self._file_fetcher,
                http=http,
            )
        try:
            await session.initialize()
        except Exception:
            await session.aclose()
            raise
        return session

    async def connect_memory(
        self,
        server_name: str,
        transport: MessageTransport,
    ) -> McpSession:
        """Connect via an injected message transport (unit tests / in-process fake)."""
        self._lookup(server_name)
        session = McpSession(
            config=self._config,
            server_name=server_name,
            file_fetcher=self._file_fetcher,
            memory=transport,
        )
        try:
            await session.initialize()
        except Exception:
            await session.aclose()
            raise
        return session

    def _lookup(self, server_name: str) -> StdioServerConfig | StreamableHttpServerConfig:
        for server in self._config.servers:
            if server.name == server_name:
                return server
        raise McpConfigError(f"server {server_name!r} is not in the configured profile list")
