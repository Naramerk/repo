"""Strict JSON for MCP wire: no NaN/Infinity, bounded ints, typed id equality."""

from __future__ import annotations

import json
import sys
from typing import Any

from coscientist.mcp.errors import IntegerDomainError, McpProtocolError

# Inclusive IEEE-754 / RFC 8785 safe-integer range (rfc8785._impl._INT_MAX/_INT_MIN).
# Interoperable JSON integers are [-(2**53)+1, 2**53-1]; ±2**53 are out of domain.
_INT_MAX = 2**53 - 1
_INT_MIN = -(2**53) + 1


def _max_int_digits() -> int:
    """Digit cap from the interpreter. Refuse when unlimited (0) — no hardcoded fallback."""
    limit = sys.get_int_max_str_digits()
    if limit == 0:
        raise ValueError(
            "sys.int_max_str_digits is 0 (unlimited); refused without an explicit digit cap"
        )
    return limit


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON number {name!r} is not allowed")


def _parse_int(literal: str) -> int:
    if len(literal) > _max_int_digits():
        raise ValueError("JSON integer exceeds maximum digit length")
    value = int(literal)
    if value < _INT_MIN or value > _INT_MAX:
        raise IntegerDomainError(
            f"JSON integer outside ±2^53-1 domain: {value}",
            value=value,
        )
    return value


def loads_strict(raw: bytes) -> Any:
    """Parse UTF-8 JSON; reject NaN/Infinity, oversized digit strings, and out-of-domain ints."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise McpProtocolError("response is not JSON", raw_bytes=raw) from exc
    try:
        _max_int_digits()  # refuse up front when interpreter digit cap is unlimited
    except ValueError as exc:
        raise McpProtocolError(str(exc), raw_bytes=raw) from exc
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
