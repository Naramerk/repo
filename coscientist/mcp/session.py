"""JSON-RPC MCP session: initialize, list_tools, call_tool."""

from __future__ import annotations

import json
from typing import Any

from coscientist.mcp.config import McpClientConfig
from coscientist.mcp.digest import content_digest
from coscientist.mcp.errors import McpProtocolError, McpTimeoutError, McpTransportError
from coscientist.mcp.files import fetch_files
from coscientist.mcp.http_transport import StreamableHttpSession
from coscientist.mcp.stdio import StdioTransport
from coscientist.mcp.transport import FileFetcher, MessageTransport
from coscientist.mcp.types import CallToolReply, ListToolsReply, McpTool
from coscientist.mcp.urls import uris_from_call_result


class McpSession:
    """Connected MCP server session (stdio message transport or streamable HTTP)."""

    def __init__(
        self,
        *,
        config: McpClientConfig,
        server_name: str,
        file_fetcher: FileFetcher,
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
        self._stdio = stdio
        self._http = http
        self._memory = memory
        self._next_id = 1
        self._initialized = False

    @property
    def server_name(self) -> str:
        return self._server_name

    async def initialize(self) -> None:
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
        )
        negotiated = result.get("protocolVersion")
        if negotiated != self._config.protocol_version:
            raise McpProtocolError("server protocolVersion does not match configured protocol_version")
        await self._notify("notifications/initialized")
        self._initialized = True

    async def list_tools(self) -> ListToolsReply:
        self._require_initialized()
        result, raw = await self._rpc(
            "tools/list",
            {},
            timeout_s=self._config.list_tools_timeout_s,
        )
        tools = _parse_tools(result)
        digest = content_digest(result)
        return ListToolsReply(
            content_digest=digest,
            raw_bytes=raw,
            result=result,
            files=(),
            tools=tools,
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolReply:
        self._require_initialized()
        result, raw = await self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout_s=self._config.call_tool_timeout_s,
        )
        is_error = result.get("isError")
        if type(is_error) is not bool:
            raise McpProtocolError("tools/call result isError must be a bool")
        uris = uris_from_call_result(
            result,
            allowed_schemes=frozenset(self._config.fetch_uri_schemes),
        )
        files = await fetch_files(
            uris,
            fetcher=self._file_fetcher,
            max_bytes=self._config.max_file_bytes,
            timeout_s=self._config.file_fetch_timeout_s,
        )
        digest = content_digest(result)
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

    async def _notify(self, method: str) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if self._http is not None:
            # Streamable HTTP: notifications are POSTed; response body is ignored.
            await self._http.request(message, timeout_s=self._config.connect_timeout_s)
            return
        await self._write(message)

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_s: float,
    ) -> tuple[dict[str, Any], bytes]:
        req_id = self._next_id
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        if self._http is not None:
            message, raw = await self._http.request(request, timeout_s=timeout_s)
            return _extract_result(message, req_id=req_id, raw=raw), raw
        await self._write(request)
        raw = await self._read(timeout_s=timeout_s)
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpProtocolError("response is not JSON", raw_bytes=raw) from exc
        if not isinstance(message, dict):
            raise McpProtocolError("JSON-RPC message must be an object", raw_bytes=raw)
        return _extract_result(message, req_id=req_id, raw=raw), raw

    async def _write(self, message: dict[str, Any]) -> None:
        raw = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if self._stdio is not None:
            await self._stdio.send(raw)
            return
        if self._memory is not None:
            await self._memory.send(raw)
            return
        raise McpTransportError("no message transport", retryable=False)

    async def _read(self, *, timeout_s: float) -> bytes:
        max_bytes = self._config.max_response_bytes
        if self._stdio is not None:
            return await self._stdio.receive(max_bytes=max_bytes, timeout_s=timeout_s)
        if self._memory is not None:
            try:
                return await self._memory.receive(max_bytes=max_bytes, timeout_s=timeout_s)
            except TimeoutError as exc:
                raise McpTimeoutError("memory transport receive exceeded timeout") from exc
        raise McpTransportError("no message transport", retryable=False)


def _extract_result(message: dict[str, Any], *, req_id: int, raw: bytes) -> dict[str, Any]:
    if message.get("jsonrpc") != "2.0":
        raise McpProtocolError("JSON-RPC version must be 2.0", raw_bytes=raw)
    if message.get("id") != req_id:
        raise McpProtocolError("JSON-RPC id mismatch", raw_bytes=raw)
    if "error" in message:
        err = message["error"]
        if not isinstance(err, dict):
            raise McpProtocolError("JSON-RPC error must be an object", raw_bytes=raw)
        code = err.get("code")
        # Message text may contain server detail — do not echo into our str beyond code.
        raise McpProtocolError(f"JSON-RPC error code {code!r}", raw_bytes=raw)
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
