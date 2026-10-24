"""Streamable HTTP MCP transport (spec 2025-06-18) over an injected HttpTransport."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

from coscientist.mcp.errors import (
    McpConfigError,
    McpProtocolError,
    McpResponseLimitError,
    McpTimeoutError,
    McpTransportError,
)
from coscientist.mcp.transport import HttpStream, HttpTransport


class StreamableHttpSession:
    """Streamable HTTP session: Mcp-Session-Id, MCP-Protocol-Version, full SSE consume."""

    def __init__(
        self,
        *,
        url: str,
        api_key_env: str | None,
        http: HttpTransport,
        max_response_bytes: int,
        read_chunk_bytes: int,
        request_ok_statuses: frozenset[int],
        notification_ok_statuses: frozenset[int],
        protocol_version: str,
    ) -> None:
        self._url = url
        self._api_key_env = api_key_env
        self._http = http
        self._max_response_bytes = max_response_bytes
        self._read_chunk_bytes = read_chunk_bytes
        self._request_ok_statuses = request_ok_statuses
        self._notification_ok_statuses = notification_ok_statuses
        self._protocol_version = protocol_version
        self._session_id: str | None = None
        self._closed = False
        self._negotiated_version: str | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def set_negotiated_version(self, version: str) -> None:
        self._negotiated_version = version

    def clear_session(self) -> None:
        self._session_id = None

    async def request(
        self,
        payload: dict[str, Any],
        *,
        timeout_s: float,
    ) -> tuple[dict[str, Any] | None, bytes, int]:
        """POST one JSON-RPC message.

        Returns ``(message_or_None, raw_bytes, status_code)``.
        Notifications expect configured notification statuses and empty body.
        """
        if self._closed:
            raise McpTransportError("streamable HTTP session is closed", retryable=False)
        is_notification = "id" not in payload
        headers = self._headers(include_session=payload.get("method") != "initialize")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            stream = await self._http.open(
                "POST",
                self._url,
                headers=headers,
                body=body,
                timeout_s=timeout_s,
                connect_ip=None,
            )
        except TimeoutError as exc:
            raise McpTimeoutError("streamable HTTP request exceeded configured timeout") from exc
        try:
            status = stream.status_code
            if is_notification:
                if status not in self._notification_ok_statuses:
                    raise McpTransportError(
                        f"streamable HTTP notification status {status}",
                        retryable=False,
                    )
                raw = await _read_limited(
                    stream,
                    max_bytes=self._max_response_bytes,
                    chunk_bytes=self._read_chunk_bytes,
                )
                if raw:
                    raise McpProtocolError(
                        "streamable HTTP notification response must have empty body",
                        raw_bytes=raw,
                    )
                return None, b"", status

            if status not in self._request_ok_statuses:
                return (
                    None,
                    await _read_limited(
                        stream,
                        max_bytes=self._max_response_bytes,
                        chunk_bytes=self._read_chunk_bytes,
                    ),
                    status,
                )

            session_hdr = _header(stream.headers, "mcp-session-id")
            if session_hdr is not None and payload.get("method") == "initialize":
                self._session_id = session_hdr

            content_type = _header(stream.headers, "content-type")
            if content_type is None:
                raise McpProtocolError("streamable HTTP response missing Content-Type")
            media = content_type.split(";", 1)[0].strip().lower()
            raw = await _read_limited(
                stream,
                max_bytes=self._max_response_bytes,
                chunk_bytes=self._read_chunk_bytes,
            )
            if media == "application/json":
                return _parse_json_rpc(raw), raw, status
            if media == "text/event-stream":
                message, event_raw = _consume_sse_for_response(raw, req_id=payload.get("id"))
                return message, event_raw, status
            raise McpProtocolError(f"unsupported streamable HTTP Content-Type: {media!r}")
        finally:
            await stream.aclose()

    async def aclose(self) -> None:
        self._closed = True

    def _headers(self, *, include_session: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        version = self._negotiated_version or self._protocol_version
        headers["MCP-Protocol-Version"] = version
        if include_session and self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        if self._api_key_env is not None:
            headers["Authorization"] = f"Bearer {_read_secret(self._api_key_env)}"
        return headers


async def _read_limited(
    stream: HttpStream,
    *,
    max_bytes: int,
    chunk_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        piece = await stream.read(min(chunk_bytes, max_bytes - total + 1))
        if not piece:
            break
        total += len(piece)
        if total > max_bytes:
            raise McpResponseLimitError(
                "HTTP body exceeds max_response_bytes while streaming",
                limit=max_bytes,
            )
        chunks.append(piece)
    return b"".join(chunks)


def _read_secret(env_name: str) -> str:
    value = os.environ.get(env_name)
    if value is None or value == "":
        raise McpConfigError(f"environment variable {env_name!r} is missing or empty")
    return value


def _header(headers: Mapping[str, str], name: str) -> str | None:
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


def _consume_sse_for_response(body: bytes, *, req_id: Any) -> tuple[dict[str, Any], bytes]:
    """Parse the full SSE body; return the JSON-RPC response matching ``req_id``."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise McpProtocolError("SSE body is not UTF-8", raw_bytes=body) from exc
    events = _parse_sse_events(text)
    if not events:
        raise McpProtocolError("SSE body has no events", raw_bytes=body)
    matched: dict[str, Any] | None = None
    matched_raw: bytes | None = None
    for data in events:
        raw = data.encode("utf-8")
        message = _parse_json_rpc(raw)
        # Skip notifications (no id) and server requests (have method+id) — caller
        # session loop handles those on stdio; on HTTP SSE they may appear before
        # the response. Server requests on SSE are answered by the session layer
        # only for stdio; here we skip non-responses until matching id.
        if "method" in message and "id" in message:
            continue
        if "id" not in message:
            continue
        if message.get("id") != req_id:
            continue
        matched = message
        matched_raw = raw
        # Continue to consume remaining events (full stream).
    if matched is None or matched_raw is None:
        raise McpProtocolError(
            "SSE stream ended without JSON-RPC response for request id",
            raw_bytes=body,
        )
    return matched, matched_raw


def _parse_sse_events(text: str) -> list[str]:
    events: list[str] = []
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "":
            if data_lines:
                events.append("\n".join(data_lines))
                data_lines = []
        # ignore event:/id:/retry: and comments
    if data_lines:
        events.append("\n".join(data_lines))
    return events
