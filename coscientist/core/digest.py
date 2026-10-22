"""Canonical digests via RFC 8785 JCS."""

from __future__ import annotations

import hashlib
from typing import Any

import rfc8785


def canonical_json(obj: Any) -> bytes:
    return rfc8785.dumps(obj)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def payload_digest(payload: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(payload))


def spec_digest(spec: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(spec))
