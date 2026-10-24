"""Strict JSON for MCP wire: no NaN/Infinity, bounded ints, typed id equality."""

from __future__ import annotations

import json
from typing import Any

from coscientist.mcp.errors import McpProtocolError

# Reject pathological int strings (DoS); well above JSON-RPC id / cursor sizes.
_MAX_INT_DIGITS = 4300


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON number {name!r} is not allowed")


def _parse_int(literal: str) -> int:
    if len(literal) > _MAX_INT_DIGITS:
        raise ValueError("JSON integer exceeds maximum digit length")
    return int(literal)


def loads_strict(raw: bytes) -> Any:
    """Parse UTF-8 JSON; reject NaN/Infinity and oversized integers."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise McpProtocolError("response is not JSON", raw_bytes=raw) from exc
    try:
        return json.loads(
            text,
            parse_constant=_reject_constant,
            parse_int=_parse_int,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise McpProtocolError("response is not JSON", raw_bytes=raw) from exc


def dumps_strict(obj: Any) -> bytes:
    """Serialize JSON with ``allow_nan=False`` (rejects NaN/Infinity)."""
    try:
        return json.dumps(
            obj,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise McpProtocolError("JSON serialization failed") from exc


def json_rpc_ids_equal(left: Any, right: Any) -> bool:
    """JSON-RPC id equality that does not coerce bool/int/float across types."""
    if type(left) is not type(right):
        return False
    return left == right
