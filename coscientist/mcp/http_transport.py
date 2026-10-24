"""Streamable HTTP MCP transport over an injected :class:`HttpTransport`."""

from __future__ import annotations

import json
import os
from typing import Any

from coscientist.mcp.errors import (
    McpConfigError,
    McpProtocolError,
    McpResponseLimitError,
    McpTimeoutError,
    McpTransportError,
)
from coscientist.mcp.transport import HttpTransport


class StreamableHttpSession:
    """One MCP session over Streamable HTTP (JSON response body).

    Uses POST per JSON-RPC message. SSE framing is accepted only when the
    response ``Content-Type`` is ``text/event-stream``; otherwise the body must
    be a single JSON-RPC object. No content-type guessing.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key_env: str | None,
        http: HttpTransport,
        max_response_bytes: int,
    ) -> None:
        self._url = url
        self._api_key_env = api_key_env
        self._http = http
        self._max_response_bytes = max_response_bytes
        self._closed = False

    async def request(
        self,
        payload: dict[str, Any],
        *,
        timeout_s: float,
    ) -> tuple[dict[str, Any], bytes]:
        if self._closed:
            raise McpTransportError("streamable HTTP session is closed", retryable=False)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._api_key_env is not None:
            headers["Authorization"] = f"Bearer {_read_secret(self._api_key_env)}"
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            response = await self._http.request(
                "POST",
                self._url,
                headers=headers,
                body=body,
                timeout_s=timeout_s,
            )
        except TimeoutError as exc:
            raise McpTimeoutError("streamable HTTP request exceeded configured timeout") from exc
        if len(response.body) > self._max_response_bytes:
            raise McpResponseLimitError(
                "streamable HTTP response exceeds max_response_bytes",
                limit=self._max_response_bytes,
            )
        # Accepted statuses for Streamable HTTP request/notification POSTs.
        if response.status_code not in (200, 202):
            raise McpTransportError(
                f"streamable HTTP status {response.status_code}",
                retryable=False,
            )
        # Notifications (no id) may get an empty accepted body.
        if "id" not in payload:
            if not response.body:
                return {}, b""
        content_type = _header(response.headers, "content-type")
        if content_type is None:
            raise McpProtocolError("streamable HTTP response missing Content-Type")
        media = content_type.split(";", 1)[0].strip().lower()
        if media == "application/json":
            return _parse_json_rpc(response.body), response.body
        if media == "text/event-stream":
            message = _first_sse_data(response.body)
            return _parse_json_rpc(message), message
        raise McpProtocolError(f"unsupported streamable HTTP Content-Type: {media!r}")

    async def aclose(self) -> None:
        self._closed = True


def _read_secret(env_name: str) -> str:
    value = os.environ.get(env_name)
    if value is None or value == "":
        raise McpConfigError(f"environment variable {env_name!r} is missing or empty")
    return value


def _header(headers: Any, name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return None


def _parse_json_rpc(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise McpProtocolError("response is not JSON", raw_bytes=raw) from exc
    if not isinstance(data, dict):
        raise McpProtocolError("JSON-RPC message must be an object", raw_bytes=raw)
    return data


def _first_sse_data(body: bytes) -> bytes:
    """Return the first SSE ``data:`` payload bytes (joined multi-line data)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise McpProtocolError("SSE body is not UTF-8", raw_bytes=body) from exc
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "" and data_lines:
            break
    if not data_lines:
        raise McpProtocolError("SSE body has no data: field", raw_bytes=body)
    return "\n".join(data_lines).encode("utf-8")
