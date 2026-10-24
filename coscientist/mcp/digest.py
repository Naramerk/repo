"""Content digests for MCP replies. Local to this package (no coscientist.core)."""

from __future__ import annotations

import hashlib
from typing import Any

import rfc8785

from coscientist.mcp.errors import McpError


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_digest(result: dict[str, Any]) -> str:
    """JCS-canonical digest of an MCP result object (DESIGN §9 / input snapshot)."""
    try:
        canonical = rfc8785.dumps(result)
    except (TypeError, ValueError, OverflowError) as exc:
        raise McpError("JCS canonicalization failed for MCP result") from exc
    return sha256_hex(canonical)
