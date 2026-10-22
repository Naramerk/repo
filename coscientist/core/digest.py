"""Canonical digests (sha256 of JSON; RFC 8785-style key sorting)."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding (sorted keys, no whitespace).

    M0 note: full RFC 8785 JCS is deferred; sorted-keys JSON is sufficient for
    idempotency and spec digests until M2 fork JCS requirements land.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def payload_digest(payload: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(payload))


def spec_digest(spec: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(spec))
