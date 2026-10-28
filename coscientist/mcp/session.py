"""JSON-RPC MCP session: initialize, list_tools, call_tool."""

from __future__ import annotations

import logging
from typing import Any

from coscientist.mcp.config import McpClientConfig
from coscientist.mcp.digest import content_digest
from coscientist.mcp.errors import (
    IntegerDomainError,
    McpError,
    McpProtocolError,
    McpResponseLimitError,
    McpSessionClosedError,
    McpTimeoutError,
    McpTransportError,
)
from coscientist.mcp.files import fetch_files
from coscientist.mcp.http_transport import StreamableHttpSession
from coscientist.mcp.jsonutil import dumps_strict, json_rpc_ids_equal, loads_strict
from coscientist.mcp.runtime import AsyncWaitFor, Clock
from coscientist.mcp.stdio import StdioTransport
from coscientist.mcp.transport import FileFetcher, MessageTransport
from coscientist.mcp.types import CallToolReply, ListToolsReply, McpTool
from coscientist.mcp.urls import uris_from_call_result

_METHOD_NOT_FOUND = -32601
_LOG = logging.getLogger(__name__)

# Consecutive completed session recoveries without an intervening successful
# RPC before recovery refuses (no recover/fail loop). Any successful RPC
# resets the budget.
_MAX_SESSION_RECOVERIES = 8


class McpSession:
    """Connected MCP server session (stdio message transport or streamable HTTP)."""

    def __init__(
        self,
        *,
        config: McpClientConfig,
        server_name: str,
        file_fetcher: FileFetcher,
        clock: Clock,
        wait_for: AsyncWaitFor,
        stdio: StdioTransport | None = None,
        http: StreamableHttpSession | None = None,
        memory: MessageTransport | None = None,
    ) -> None:
        modes = [stdio is not None, http is not None, memory is not None]
        if sum(1 for m in modes if m) != 1:
            raise ValueError("exactly one of stdio, http, memory transport required")
        self._config = config
        self._server_name = server_name
        self._file_fetcher = file_fetcher
        self._clock = clock
        self._wait_for = wait_for
        self._stdio = stdio
        self._http = http
        self._memory = memory
        self._next_id = 1
        self._initialized = False
        self._negotiated_version: str | None = None
        self._session_recoveries = 0

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def call_tool_timeout_s(self) -> float:
        """Same timeout ``call_tool`` uses (``McpClientConfig.call_tool_timeout_s``)."""
        return self._config.call_tool_timeout_s

    async def initialize(self) -> None:
        await self._initialize_handshake(reset_recovery_budget=True)

    async def _initialize_handshake(self, *, reset_recovery_budget: bool) -> None:
        result, _raw = await self._rpc(
            "initialize",
            {
                "protocolVersion": self._config.protocol_version,
                "capabilities": {},
                "clientInfo": {
                    "name": self._config.client_name,
                    "version": self._config.client_version,
                },
            },
            timeout_s=self._config.connect_timeout_s,
            allow_session_reset=False,
            reset_recovery_budget=reset_recovery_budget,
        )
        negotiated = result.get("protocolVersion")
        if not isinstance(negotiated, str) or negotiated == "":
            raise McpProtocolError("initialize result.protocolVersion must be a non-empty string")
        if negotiated not in self._config.supported_protocol_versions:
            raise McpProtocolError(f"server protocolVersion {negotiated!r} not in supported_protocol_versions")
        self._negotiated_version = negotiated
        if self._http is not None:
            self._http.set_negotiated_version(negotiated)
        await self._notify("notifications/initialized")
        self._initialized = True

    async def list_tools(self) -> ListToolsReply:
        self._require_initialized()
        tools_acc: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        raw_pages: list[bytes] = []
        page = 0
        while True:
            page += 1
            if page > self._config.max_list_tools_pages:
                await self._fail_close()
                raise McpProtocolError(
                    f"tools/list exceeded max_list_tools_pages ({self._config.max_list_tools_pages})",
                    raw_bytes=b"".join(raw_pages),
                )
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            result, raw = await self._rpc(
                "tools/list",
                params,
                timeout_s=self._config.list_tools_timeout_s,
                session_retry=True,
            )
            raw_pages.append(raw)
            page_tools = result.get("tools")
            if not isinstance(page_tools, list):
                await self._fail_close()
                raise McpProtocolError("tools/list result.tools must be a list", raw_bytes=raw)
            tools_acc.extend(page_tools)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or next_cursor == "":
                await self._fail_close()
                raise McpProtocolError(
                    "tools/list nextCursor must be a non-empty string when present",
                    raw_bytes=raw,
                )
            if next_cursor in seen_cursors or next_cursor == cursor:
                await self._fail_close()
                raise McpProtocolError(
                    "tools/list repeated nextCursor (pagination loop)",
                    raw_bytes=raw,
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        aggregate: dict[str, Any] = {"tools": tools_acc}
        parsed = _parse_tools(aggregate)
        digest = content_digest(aggregate)
        return ListToolsReply(
            content_digest=digest,
            raw_bytes=b"".join(raw_pages),
            result=aggregate,
            files=(),
            tools=parsed,
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        retry_after_recovery: bool = False,
    ) -> CallToolReply:
        """Call one tool; ``retry_after_recovery`` gates the one-shot retry.

        If the call fails because the HTTP session is closed/expired, the
        session is recovered (reopen + initialize) regardless; the call
        itself is retried once only when the caller vouches the tool is
        idempotent (``retry_after_recovery=True``). Non-idempotent calls
        fail once and the next call rides the fresh session.
        """
        self._require_initialized()
        result, raw = await self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout_s=self._config.call_tool_timeout_s,
            session_retry=retry_after_recovery,
        )
        is_error = result.get("isError")
        if type(is_error) is not bool:
            await self._fail_close()
            raise McpProtocolError("tools/call result isError must be a bool", raw_bytes=raw)
        uris = uris_from_call_result(
            result,
            allowed_schemes=frozenset(self._config.fetch_uri_schemes),
        )
        # Digest before fetch so JCS failures never download.
        try:
            digest = content_digest(result)
        except McpError:
            await self._fail_close()
            raise
        files = await fetch_files(
            uris,
            fetcher=self._file_fetcher,
            max_bytes=self._config.max_file_bytes,
            max_files=self._config.max_files_per_call,
            max_total_bytes=self._config.max_files_total_bytes,
            timeout_s=self._config.files_fetch_timeout_s,
            file_timeout_s=self._config.file_fetch_timeout_s,
            clock=self._clock,
            wait_for=self._wait_for,
        )
        return CallToolReply(
            content_digest=digest,
            raw_bytes=raw,
            result=result,
            files=files,
            is_error=is_error,
        )

    async def aclose(self) -> None:
        if self._stdio is not None:
            await self._stdio.aclose()
        if self._http is not None:
            await self._http.aclose()
        if self._memory is not None:
            await self._memory.aclose()

    async def __aenter__(self) -> McpSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise McpTransportError("session is not initialized", retryable=False)

    async def _fail_close(self) -> None:
        if self._stdio is not None:
            self._stdio.reset_receive_buffer()
        if self._memory is not None:
            self._memory.reset_receive_buffer()
        await self.aclose()

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if self._http is not None:
            try:
                await self._wait_for(
                    self._http.request(message, timeout_s=self._config.notification_timeout_s),
                    timeout_s=self._config.notification_timeout_s,
                )
            except TimeoutError as exc:
                raise McpTimeoutError(
                    "streamable HTTP notification exceeded notification_timeout_s"
                ) from exc
            return
        await self._write(message)
        await self._drain_outbound()

    async def _cancel_and_close(self, req_id: int) -> None:
        """Best-effort cancelled notification, then always aclose.

        Failures while sending cancel are logged and swallowed so the original
        ``McpTimeoutError`` from the caller is preserved.
        """
        try:
            await self._notify(
                "notifications/cancelled",
                {"requestId": req_id, "reason": "timeout"},
            )
        except (McpError, OSError, TimeoutError):
            _LOG.exception("failed to send notifications/cancelled for request %s", req_id)
        finally:
            await self.aclose()

    async def _recover_session(self) -> None:
        """Reopen a closed streamable-HTTP transport, re-run the initialize handshake.

        Transport state recovery, not a tool retry: the closed/expired session
        is a client-side (or server-expiry) condition, so recovering here lets
        every tool on this session benefit. Bounded — at most
        ``_MAX_SESSION_RECOVERIES`` completed recoveries without an
        intervening successful RPC; past that recovery refuses with a
        transport error instead of looping.
        """
        if self._http is None:
            raise McpTransportError(
                "session recovery requires the streamable HTTP transport",
                retryable=False,
            )
        if self._session_recoveries >= _MAX_SESSION_RECOVERIES:
            raise McpTransportError(
                "session recovery budget exhausted "
                f"({_MAX_SESSION_RECOVERIES} consecutive recoveries without a successful call)",
                retryable=False,
            )
        self._http.reopen()
        self._initialized = False
        # The recovery handshake itself must not reset the recovery budget:
        # only a successful real RPC (tools/call, tools/list, ...) proves the
        # recovered session actually works.
        await self._initialize_handshake(reset_recovery_budget=False)
        self._session_recoveries += 1

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_s: float,
        allow_session_reset: bool = True,
        session_retry: bool = False,
        reset_recovery_budget: bool = True,
    ) -> tuple[dict[str, Any], bytes]:
        try:
            req_id = self._next_id
            self._next_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }
            if self._http is not None:
                pair = await self._rpc_http(
                    request,
                    req_id=req_id,
                    timeout_s=timeout_s,
                    allow_session_reset=allow_session_reset,
                    session_retry=session_retry,
                )
            else:
                await self._write(request)
                try:
                    pair = await self._read_until_response(req_id=req_id, timeout_s=timeout_s)
                except McpTimeoutError:
                    await self._cancel_and_close(req_id)
                    raise
                except (IntegerDomainError, McpResponseLimitError):
                    await self._fail_close()
                    raise
        except McpSessionClosedError:
            if not allow_session_reset:
                # initialize itself, or an already-recovered retry — never recurse.
                raise
            # Session-closed is transport state, not a tool outcome: recover,
            # then retry the call at most once and only when the caller vouches
            # the call is re-issuable (idempotent). Otherwise fail once; the
            # next call rides the fresh session.
            await self._recover_session()
            if session_retry:
                # One recovered retry; the retried call raises its own errors
                # and cannot recurse (allow_session_reset=False).
                return await self._rpc(  # audit-ok[except_silent_return]: single retry on the fresh session
                    method,
                    params,
                    timeout_s=timeout_s,
                    allow_session_reset=False,
                    session_retry=False,
                )
            raise
        if reset_recovery_budget:
            self._session_recoveries = 0
        return pair

    async def _rpc_http(
        self,
        request: dict[str, Any],
        *,
        req_id: int,
        timeout_s: float,
        allow_session_reset: bool,
        session_retry: bool,
    ) -> tuple[dict[str, Any], bytes]:
        assert self._http is not None
        try:
            message, raw, status = await self._wait_for(
                self._http.request(request, timeout_s=timeout_s),
                timeout_s=timeout_s,
            )
        except TimeoutError as exc:
            await self._cancel_and_close(req_id)
            raise McpTimeoutError("streamable HTTP request exceeded configured timeout") from exc
        except McpTimeoutError:
            await self._cancel_and_close(req_id)
            raise
        except (IntegerDomainError, McpProtocolError, McpTransportError, McpResponseLimitError):
            await self._fail_close()
            raise
        if status == 404 and allow_session_reset and self._http.session_id is not None:
            # Spec 2025-06-18: the server answers 404 for an expired/unknown
            # session id — session expiry, not a tool outcome. Recover the
            # session; retry the call only when the caller vouches it is safe.
            await self._recover_session()
            if session_retry:
                return await self._rpc(
                    str(request["method"]),
                    dict(request["params"]),
                    timeout_s=timeout_s,
                    allow_session_reset=False,
                    session_retry=False,
                )
            raise McpTransportError(f"streamable HTTP status {status}", retryable=False)
        if message is None:
            await self._fail_close()
            raise McpTransportError(
                f"streamable HTTP status {status}",
                retryable=False,
            )
        try:
            return _extract_result(message, req_id=req_id, raw=raw), raw
        except McpProtocolError:
            await self._fail_close()
            raise

    async def _read_until_response(
        self,
        *,
        req_id: int,
        timeout_s: float,
    ) -> tuple[dict[str, Any], bytes]:
        deadline = self._clock.monotonic() + timeout_s
        while True:
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0:
                raise McpTimeoutError("stdio/memory receive exceeded configured timeout")
            try:
                raw = await self._read(timeout_s=remaining)
            except McpTimeoutError:
                raise
            except (McpProtocolError, McpTransportError, McpResponseLimitError):
                await self._fail_close()
                raise
            try:
                message = loads_strict(raw)
            except (IntegerDomainError, McpProtocolError):
                await self._fail_close()
                raise
            if not isinstance(message, dict):
                await self._fail_close()
                raise McpProtocolError("JSON-RPC message must be an object", raw_bytes=raw)
            if "id" not in message:
                if "method" not in message:
                    await self._fail_close()
                    raise McpProtocolError(
                        "JSON-RPC message requires id or method",
                        raw_bytes=raw,
                    )
                continue  # notification
            if "method" in message:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": _METHOD_NOT_FOUND,
                            "message": "Method not found",
                        },
                    }
                )
                continue
            msg_id = message.get("id")
            if msg_id is None:
                await self._fail_close()
                raise McpProtocolError(
                    "JSON-RPC response id must not be null",
                    raw_bytes=raw,
                )
            if not json_rpc_ids_equal(msg_id, req_id):
                await self._fail_close()
                raise McpProtocolError(
                    "JSON-RPC response id does not match request",
                    raw_bytes=raw,
                )
            try:
                return _extract_result(message, req_id=req_id, raw=raw), raw
            except McpProtocolError:
                await self._fail_close()
                raise

    async def _write(self, message: dict[str, Any]) -> None:
        raw = dumps_strict(message)
        if b"\n" in raw:
            raise McpProtocolError("JSON-RPC message must not contain embedded newlines")
        if self._stdio is not None:
            await self._stdio.send(raw)
            return
        if self._memory is not None:
            await self._memory.send(raw)
            return
        raise McpTransportError("no message transport", retryable=False)

    async def _drain_outbound(self) -> None:
        if self._memory is not None:
            await self._memory.join_outbound()
            return
        if self._stdio is not None:
            await self._stdio.join_outbound()

    async def _read(self, *, timeout_s: float) -> bytes:
        max_bytes = self._config.max_response_bytes
        if self._stdio is not None:
            return await self._stdio.receive(max_bytes=max_bytes, timeout_s=timeout_s)
        if self._memory is not None:
            try:
                return await self._wait_for(
                    self._memory.receive(max_bytes=max_bytes, timeout_s=timeout_s),
                    timeout_s=timeout_s,
                )
            except TimeoutError as exc:
                raise McpTimeoutError("memory transport receive exceeded timeout") from exc
        raise McpTransportError("no message transport", retryable=False)


def _extract_result(message: dict[str, Any], *, req_id: int, raw: bytes) -> dict[str, Any]:
    if message.get("jsonrpc") != "2.0":
        raise McpProtocolError("JSON-RPC version must be 2.0", raw_bytes=raw)
    if not json_rpc_ids_equal(message.get("id"), req_id):
        raise McpProtocolError("JSON-RPC id mismatch", raw_bytes=raw)
    if "error" in message:
        err = message["error"]
        if not isinstance(err, dict):
            raise McpProtocolError("JSON-RPC error must be an object", raw_bytes=raw)
        code = err.get("code")
        rpc_message = err.get("message")
        rpc_data = err.get("data")
        detail = f"JSON-RPC error code {code!r}"
        if isinstance(rpc_message, str) and rpc_message:
            detail = f"{detail}: {rpc_message}"
        raise McpProtocolError(
            detail,
            raw_bytes=raw,
            rpc_code=code if isinstance(code, int) else None,
            rpc_message=rpc_message if isinstance(rpc_message, str) else None,
            rpc_data=rpc_data,
        )
    result = message.get("result")
    if not isinstance(result, dict):
        raise McpProtocolError("JSON-RPC result must be an object", raw_bytes=raw)
    return result


def _parse_tools(result: dict[str, Any]) -> tuple[McpTool, ...]:
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise McpProtocolError("tools/list result.tools must be a list")
    out: list[McpTool] = []
    for index, item in enumerate(tools):
        if not isinstance(item, dict):
            raise McpProtocolError(f"tools[{index}] must be an object")
        name = item.get("name")
        if not isinstance(name, str) or name == "":
            raise McpProtocolError(f"tools[{index}].name must be a non-empty string")
        description = item.get("description")
        if description is not None and not isinstance(description, str):
            raise McpProtocolError(f"tools[{index}].description must be a string when present")
        schema = item.get("inputSchema")
        if not isinstance(schema, dict):
            raise McpProtocolError(f"tools[{index}].inputSchema must be an object")
        out.append(McpTool(name=name, description=description, input_schema=schema))
    return tuple(out)
