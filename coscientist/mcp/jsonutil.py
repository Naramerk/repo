"""Strict JSON for MCP wire: no NaN/Infinity, bounded ints, typed id equality."""

from __future__ import annotations

import json
import sys
from typing import Any

from coscientist.mcp.errors import IntegerDomainError, McpProtocolError

# IEEE-754 / JS Number.MAX_SAFE_INTEGER — larger ints are not interoperable.
_MAX_SAFE_INTEGER = 2**53


def _max_int_digits() -> int:
    """Digit cap from the interpreter; fall back when unlimited (0)."""
    limit = sys.get_int_max_str_digits()
    return limit if limit > 0 else 4300


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON number {name!r} is not allowed")


def _parse_int(literal: str) -> int:
    if len(literal) > _max_int_digits():
        raise ValueError("JSON integer exceeds maximum digit length")
    value = int(literal)
    if value > _MAX_SAFE_INTEGER or value < -_MAX_SAFE_INTEGER:
        raise IntegerDomainError(
            f"JSON integer outside ±2^53 domain: {value}",
            value=value,
        )
    return value


def loads_strict(raw: bytes) -> Any:
    """Parse UTF-8 JSON; reject NaN/Infinity, oversized digit strings, and ±2^53 ints."""
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
    except IntegerDomainError as exc:
        raise IntegerDomainError(exc.detail, value=exc.value, raw_bytes=raw) from exc
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
