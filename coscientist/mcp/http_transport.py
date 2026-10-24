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

_METHOD_NOT_FOUND = -32601


class StreamableHttpSession:
    """Streamable HTTP session: Mcp-Session-Id, MCP-Protocol-Version, incremental SSE."""

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
            if media == "application/json":
                raw = await _read_limited(
                    stream,
                    max_bytes=self._max_response_bytes,
                    chunk_bytes=self._read_chunk_bytes,
                )
                return _parse_json_rpc(raw), raw, status
            if media == "text/event-stream":
                message, event_raw = await self._consume_sse(
                    stream,
                    req_id=payload.get("id"),
                    timeout_s=timeout_s,
                )
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

    async def _consume_sse(
        self,
        stream: HttpStream,
        *,
        req_id: Any,
        timeout_s: float,
    ) -> tuple[dict[str, Any], bytes]:
        """Incrementally parse SSE; answer server requests; return on first matching id."""
        parser = _SseParser()
        total = 0
        matched: dict[str, Any] | None = None
        matched_raw: bytes | None = None
        while True:
            piece = await stream.read(self._read_chunk_bytes)
            if not piece:
                break
            total += len(piece)
            if total > self._max_response_bytes:
                raise McpResponseLimitError(
                    "HTTP SSE body exceeds max_response_bytes while streaming",
                    limit=self._max_response_bytes,
                )
            for data in parser.feed(piece):
                raw = data.encode("utf-8")
                message = _parse_json_rpc(raw)
                if "method" in message and "id" in message:
                    await self._answer_method_not_found(message["id"], timeout_s=timeout_s)
                    continue
                if "id" not in message:
                    continue
                if message.get("id") != req_id:
                    continue
                if matched is not None:
                    raise McpProtocolError(
                        "SSE stream contained duplicate JSON-RPC response id",
                        raw_bytes=raw,
                    )
                matched = message
                matched_raw = raw
            if matched is not None and matched_raw is not None:
                # Return once the matching response is in hand (do not read to EOF).
                return matched, matched_raw
        if matched is None or matched_raw is None:
            raise McpProtocolError(
                "SSE stream ended without JSON-RPC response for request id",
                raw_bytes=b"",
            )
        return matched, matched_raw

    async def _answer_method_not_found(self, req_id: Any, *, timeout_s: float) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": _METHOD_NOT_FOUND, "message": "Method not found"},
        }
        headers = self._headers(include_session=True)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        stream = await self._http.open(
            "POST",
            self._url,
            headers=headers,
            body=body,
            timeout_s=timeout_s,
            connect_ip=None,
        )
        try:
            # Best-effort: drain/close; status is not part of the client request result.
            await _read_limited(
                stream,
                max_bytes=self._max_response_bytes,
                chunk_bytes=self._read_chunk_bytes,
            )
        finally:
            await stream.aclose()


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


class _SseParser:
    """Incremental SSE ``data:`` event parser (UTF-8 chunks)."""

    def __init__(self) -> None:
        self._buf = ""
        self._data_lines: list[str] = []

    def feed(self, chunk: bytes) -> list[str]:
        try:
            self._buf += chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise McpProtocolError("SSE body is not UTF-8", raw_bytes=chunk) from exc
        events: list[str] = []
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line = self._buf[:nl]
            self._buf = self._buf[nl + 1 :]
            if line.endswith("\r"):
                line = line[:-1]
            if line.startswith("data:"):
                self._data_lines.append(line[5:].lstrip())
            elif line == "":
                if self._data_lines:
                    events.append("\n".join(self._data_lines))
                    self._data_lines = []
            # ignore event:/id:/retry: and comments
        return events
