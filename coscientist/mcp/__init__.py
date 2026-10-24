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
    McpTimeoutError,
    McpTransportError,
    McpUnsupportedUriError,
)
from coscientist.mcp.files import HttpFileFetcher
from coscientist.mcp.types import CallToolReply, FetchedFile, ListToolsReply, McpReply, McpTool

__all__ = [
    "CallToolReply",
    "FetchedFile",
    "HttpFileFetcher",
    "ListToolsReply",
    "McpClient",
    "McpClientConfig",
    "McpConfigError",
    "McpError",
    "McpProtocolError",
    "McpReply",
    "McpResponseLimitError",
    "McpTimeoutError",
    "McpTool",
    "McpTransportError",
    "McpUnsupportedUriError",
    "StdioServerConfig",
    "StreamableHttpServerConfig",
]
