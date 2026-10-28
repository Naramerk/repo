"""Typed MCP client errors. No string matching or shape heuristics."""

from __future__ import annotations

from typing import Any


class McpError(Exception):
    """Base class for MCP client failures."""


class McpConfigError(McpError):
    """Missing or invalid configuration / secret environment variable."""


class McpTimeoutError(McpError):
    """Connect, request, terminate, or file-fetch exceeded a configured timeout."""


class McpTransportError(McpError):
    """Transport failed before a usable MCP response was obtained."""

    def __init__(self, detail: str, *, retryable: bool) -> None:
        self.detail = detail
        self.retryable = retryable
        super().__init__(detail)


class McpSessionClosedError(McpTransportError):
    """Streamable HTTP transport session is closed (client-side state).

    Typed so the session layer can recover (reopen + re-initialize) without
    string matching on the error detail. Nothing about the failed call is
    known to have reached the server.
    """


class McpProtocolError(McpError):
    """JSON-RPC / MCP protocol failure (parse error, RPC error, bad shape)."""

    def __init__(
        self,
        detail: str,
        *,
        raw_bytes: bytes | None = None,
        rpc_code: int | None = None,
        rpc_message: str | None = None,
        rpc_data: Any = None,
    ) -> None:
        self.detail = detail
        self.raw_bytes = raw_bytes
        self.rpc_code = rpc_code
        self.rpc_message = rpc_message
        self.rpc_data = rpc_data
        super().__init__(detail)


class IntegerDomainError(McpError):
    """JSON integer outside the interoperable ±(2^53−1) RFC 8785 domain."""

    def __init__(self, detail: str, *, value: int, raw_bytes: bytes | None = None) -> None:
        self.detail = detail
        self.value = value
        self.raw_bytes = raw_bytes
        super().__init__(detail)


class McpResponseLimitError(McpError):
    """Response or downloaded file exceeded a configured byte limit."""

    def __init__(self, detail: str, *, limit: int) -> None:
        self.detail = detail
        self.limit = limit
        super().__init__(detail)


class McpUnsupportedUriError(McpError):
    """MCP reply referenced a URI scheme this client will not fetch."""

    def __init__(self, detail: str, *, uri: str) -> None:
        self.detail = detail
        self.uri = uri
        super().__init__(detail)


class McpSsrfError(McpError):
    """URL failed SSRF policy (scheme, private IP, DNS rebinding, redirect)."""

    def __init__(self, detail: str, *, uri: str) -> None:
        self.detail = detail
        self.uri = uri
        super().__init__(detail)
