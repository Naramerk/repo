"""Content digests for MCP replies. Local to this package (no coscientist.core)."""

from __future__ import annotations

import hashlib
from typing import Any

import rfc8785


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_digest(result: dict[str, Any]) -> str:
    """JCS-canonical digest of an MCP result object (DESIGN §9 / input snapshot)."""
    return sha256_hex(rfc8785.dumps(result))
