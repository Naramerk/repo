"""Streamable HTTP MCP transport (spec 2025-06-18) over an injected HttpTransport."""

from __future__ import annotations

import codecs
import json
import os
import time
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
        close_timeout_s: float,
    ) -> None:
        self._url = url
        self._api_key_env = api_key_env
        self._http = http
        self._max_response_bytes = max_response_bytes
        self._read_chunk_bytes = read_chunk_bytes
        self._request_ok_statuses = request_ok_statuses
        self._notification_ok_statuses = notification_ok_statuses
        self._protocol_version = protocol_version
        self._close_timeout_s = close_timeout_s
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
        deadline = time.monotonic() + timeout_s
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
                    deadline=deadline,
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
                        deadline=deadline,
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
                    deadline=deadline,
                )
                return _parse_json_rpc(raw), raw, status
            if media == "text/event-stream":
                message, event_raw = await self._consume_sse(
                    stream,
                    req_id=payload.get("id"),
                    deadline=deadline,
                )
                return message, event_raw, status
            raise McpProtocolError(f"unsupported streamable HTTP Content-Type: {media!r}")
        finally:
            await stream.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        session_id = self._session_id
        self._closed = True
        if session_id is None:
            return
        headers = self._headers(include_session=True)
        # Session id was cleared from include only when None; force the id we had.
        headers["Mcp-Session-Id"] = session_id
        try:
            stream = await self._http.open(
                "DELETE",
                self._url,
                headers=headers,
                body=None,
                timeout_s=self._close_timeout_s,
                connect_ip=None,
            )
        except (McpTimeoutError, McpTransportError, TimeoutError):
            return
        try:
            await _read_limited(
                stream,
                max_bytes=self._max_response_bytes,
                chunk_bytes=self._read_chunk_bytes,
                deadline=time.monotonic() + self._close_timeout_s,
            )
        except (McpResponseLimitError, McpTimeoutError, McpTransportError):
            pass
        finally:
            await stream.aclose()

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
        deadline: float,
    ) -> tuple[dict[str, Any], bytes]:
        """Incrementally parse SSE; answer server requests; return on first matching id.

        Overall ``deadline`` is absolute (monotonic). Keepalive comments must not
        extend it — each read uses the remaining time only.
        """
        parser = _SseParser()
        total = 0
        matched: dict[str, Any] | None = None
        matched_raw: bytes | None = None
        while True:
            _remaining(deadline)
            piece = await stream.read(self._read_chunk_bytes)
            if not piece:
                events = parser.finish()
            else:
                if time.monotonic() >= deadline:
                    raise McpTimeoutError("streamable HTTP SSE exceeded configured timeout")
                total += len(piece)
                if total > self._max_response_bytes:
                    raise McpResponseLimitError(
                        "HTTP SSE body exceeds max_response_bytes while streaming",
                        limit=self._max_response_bytes,
                    )
                events = parser.feed(piece)
            for data in events:
                raw = data.encode("utf-8")
                message = _parse_json_rpc(raw)
                if "method" in message and "id" in message:
                    await self._answer_method_not_found(
                        message["id"],
                        timeout_s=_remaining(deadline),
                    )
                    continue
                if "id" not in message:
                    if "method" not in message:
                        raise McpProtocolError(
                            "JSON-RPC message requires id or method",
                            raw_bytes=raw,
                        )
                    continue  # notification
                msg_id = message.get("id")
                if msg_id is None:
                    raise McpProtocolError(
                        "SSE JSON-RPC response id must not be null",
                        raw_bytes=raw,
                    )
                if msg_id != req_id:
                    raise McpProtocolError(
                        "SSE JSON-RPC response id does not match request",
                        raw_bytes=raw,
                    )
                if matched is not None:
                    raise McpProtocolError(
                        "SSE stream contained duplicate JSON-RPC response id",
                        raw_bytes=raw,
                    )
                matched = message
                matched_raw = raw
            # Return after finishing this feed batch so duplicates in the same
            # batch (incl. events completed across prior chunks) are detected.
            if matched is not None and matched_raw is not None:
                return matched, matched_raw
            if not piece:
                break
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
            await _read_limited(
                stream,
                max_bytes=self._max_response_bytes,
                chunk_bytes=self._read_chunk_bytes,
                deadline=time.monotonic() + timeout_s,
            )
        finally:
            await stream.aclose()


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise McpTimeoutError("streamable HTTP request exceeded configured timeout")
    return left


async def _read_limited(
    stream: HttpStream,
    *,
    max_bytes: int,
    chunk_bytes: int,
    deadline: float,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        _remaining(deadline)
        piece = await stream.read(min(chunk_bytes, max_bytes - total + 1))
        if not piece:
            break
        if time.monotonic() >= deadline:
            raise McpTimeoutError("streamable HTTP body read exceeded configured timeout")
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
    """Incremental SSE ``data:`` event parser with incremental UTF-8 decode."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._buf = ""
        self._data_lines: list[str] = []

    def feed(self, chunk: bytes) -> list[str]:
        try:
            self._buf += self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError as exc:
            raise McpProtocolError("SSE body is not UTF-8", raw_bytes=chunk) from exc
        return self._drain_lines()

    def finish(self) -> list[str]:
        try:
            self._buf += self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise McpProtocolError("SSE body is not UTF-8", raw_bytes=b"") from exc
        return self._drain_lines(flush_cr=True)

    def _drain_lines(self, *, flush_cr: bool = False) -> list[str]:
        events: list[str] = []
        while True:
            lf = self._buf.find("\n")
            cr = self._buf.find("\r")
            if lf < 0 and cr < 0:
                break
            if lf >= 0 and (cr < 0 or lf < cr):
                line = self._buf[:lf]
                if line.endswith("\r"):
                    line = line[:-1]
                self._buf = self._buf[lf + 1 :]
                events.extend(self._handle_line(line))
                continue
            # CR at end of buffer: hold — next chunk may supply LF (CRLF split).
            # On EOF (flush_cr) or when the next byte is not LF, treat as CR-only.
            if cr == len(self._buf) - 1:
                if not flush_cr:
                    break
                line = self._buf[:cr]
                self._buf = self._buf[cr + 1 :]
                events.extend(self._handle_line(line))
                continue
            if self._buf[cr + 1] == "\n":
                line = self._buf[:cr]
                self._buf = self._buf[cr + 2 :]
            else:
                # CR not followed by LF → CR-only line ending (SSE allows this).
                line = self._buf[:cr]
                self._buf = self._buf[cr + 1 :]
            events.extend(self._handle_line(line))
        return events

    def _handle_line(self, line: str) -> list[str]:
        if line.startswith("data:"):
            payload = line[5:]
            if payload.startswith(" "):
                payload = payload[1:]
            self._data_lines.append(payload)
            return []
        if line == "":
            if self._data_lines:
                event = "\n".join(self._data_lines)
                self._data_lines = []
                return [event]
            return []
        # ignore event:/id:/retry: and comment lines (":" keepalive)
        return []
