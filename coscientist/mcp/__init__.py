"""Thin MCP client for the agent/tools layer (M2 prep for A7).

Does not import ``coscientist.core``. Secrets are read only from environment
variables named in config. This package does not log or print credentials or
response bodies.
"""

from __future__ import annotations

from coscientist.mcp.client import McpClient
from coscientist.mcp.config import (
    McpClientConfig,
    StdioServerConfig,
    StreamableHttpServerConfig,
)
from coscientist.mcp.errors import (
    McpConfigError,
    McpError,
    McpProtocolError,
    McpResponseLimitError,
    McpSsrfError,
    McpTimeoutError,
    McpTransportError,
    McpUnsupportedUriError,
)
from coscientist.mcp.files import SafeHttpFileFetcher
from coscientist.mcp.httpx_transport import HttpxTransport
from coscientist.mcp.types import CallToolReply, FetchedFile, ListToolsReply, McpReply, McpTool

__all__ = [
    "CallToolReply",
    "FetchedFile",
    "HttpxTransport",
    "ListToolsReply",
    "McpClient",
    "McpClientConfig",
    "McpConfigError",
    "McpError",
    "McpProtocolError",
    "McpReply",
    "McpResponseLimitError",
    "McpSsrfError",
    "McpTimeoutError",
    "McpTool",
    "McpTransportError",
    "McpUnsupportedUriError",
    "SafeHttpFileFetcher",
    "StdioServerConfig",
    "StreamableHttpServerConfig",
]
