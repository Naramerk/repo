"""Bounded HTTP/JSON handling for the injected remote coder transport."""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from coscientist.core.events.clock import Clock
from coscientist.mcp.errors import McpError
from coscientist.mcp.transport import HttpTransport
from coscientist.tools.coder.remote_contract import HttpReceipt, RemoteCoderError, endpoint


@dataclass(frozen=True)
class RemoteLimits:
    request_bytes: int
    response_bytes: int
    download_bytes: int
    read_chunk_bytes: int
    json_depth: int
    timeout_s: float
    close_timeout_s: float

    def __post_init__(self) -> None:
        for value in (
            self.request_bytes,
            self.response_bytes,
            self.download_bytes,
            self.read_chunk_bytes,
            self.json_depth,
        ):
            if type(value) is not int or value <= 0:
                raise RemoteCoderError("remote_limits_invalid")
        if self.json_depth > 128 or any(not math.isfinite(value) or value <= 0
                                      for value in (self.timeout_s, self.close_timeout_s)):
            raise RemoteCoderError("remote_limits_invalid")


def parse_object(raw: bytes, *, max_depth: int) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
        depth = 0
        quoted = escaped = False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > max_depth:
                    raise RemoteCoderError("remote_json_depth_exceeded")
            elif char in "]}":
                depth -= 1

        def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in rows:
                if key in result:
                    raise RemoteCoderError("remote_json_duplicate_key")
                result[key] = value
            return result

        def number(value: str) -> float:
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError("nonfinite JSON")
            return parsed

        def reject_constant(value: str) -> None:
            raise ValueError("nonfinite JSON")

        value = json.loads(text, object_pairs_hook=pairs, parse_float=number, parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise RemoteCoderError("remote_json_object_required")
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RemoteCoderError("remote_json_invalid", kind="opaque") from exc


class RemoteWire:
    def __init__(self, *, base_url: str, http: HttpTransport, clock: Clock,
                 headers: dict[str, str], limits: RemoteLimits) -> None:
        self.base_url = endpoint(base_url)
        self.http = http
        self.clock = clock
        self.headers = dict(headers)
        self.limits = limits
        if any("\r" in key + value or "\n" in key + value for key, value in self.headers.items()):
            raise RemoteCoderError("remote_headers_invalid")

    async def exchange(self, method: str, path: str, *, body: bytes | None,
                       deadline: datetime, operation: str, max_bytes: int) -> HttpReceipt:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise RemoteCoderError("remote_deadline_naive")
        remaining = (deadline - self.clock.now()).total_seconds()
        if remaining <= 0:
            raise RemoteCoderError("remote_deadline_elapsed")
        if body is not None and len(body) > self.limits.request_bytes:
            raise RemoteCoderError("remote_request_exceeded")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise RemoteCoderError("remote_response_budget_invalid")
        headers = {**self.headers, "Accept-Encoding": "identity"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with asyncio.timeout(min(remaining, self.limits.timeout_s)):
                stream = await self.http.open(
                    method,
                    self.base_url + "/api/v1/" + path,
                    headers=headers,
                    body=body,
                    timeout_s=min(remaining, self.limits.timeout_s),
                    connect_ip=None,
                )
                try:
                    if 300 <= stream.status_code < 400:
                        raise RemoteCoderError("remote_redirect_refused", kind="opaque")
                    encoding = None
                    for key, value in stream.headers.items():
                        if key.lower() == "content-encoding":
                            encoding = value
                            break
                    if encoding not in (None, "identity"):
                        raise RemoteCoderError("remote_encoding_refused", kind="opaque")
                    chunks = []
                    size = 0
                    while True:
                        chunk = await stream.read(min(self.limits.read_chunk_bytes, max_bytes - size + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise RemoteCoderError("remote_response_exceeded", kind="opaque")
                        chunks.append(chunk)
                    return HttpReceipt(operation, stream.status_code, b"".join(chunks))
                finally:
                    await asyncio.wait_for(stream.aclose(), timeout=self.limits.close_timeout_s)
        except (McpError, OSError, TimeoutError) as exc:
            raise RemoteCoderError("remote_transport_uncertain", kind="transport_sent") from exc
