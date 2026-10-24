"""MCP server facade: publish CoScientist modules as MCP tools (DESIGN §6.5).

Speaks JSON-RPC tools/list + tools/call over in-process messages. Handlers are
injected ``ModuleHandler`` Protocols (API wiring / fakes) — no ``coscientist.mcp``
client import (import-linter: api/tools ↛ mcp).
"""

from __future__ import annotations

import json
from typing import Any

from coscientist.integrations.config import McpServerFacadeConfig
from coscientist.integrations.errors import (
    IntegrationDisabled,
    IntegrationProtocolError,
)
from coscientist.integrations.ports import ModuleHandler

MCP_SERVER_INTEGRATION_NAME = "mcp_server"

# JSON-RPC 2.0 reserved / server error codes (spec; not domain heuristics).
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_SERVER_ERROR = -32000


class McpModuleServer:
    """Optional MCP server exposing configured modules as tools."""

    def __init__(
        self,
        config: McpServerFacadeConfig,
        *,
        handlers: dict[str, ModuleHandler],
    ) -> None:
        self._config = config
        expected = {m.name for m in config.modules}
        provided = set(handlers)
        if expected != provided:
            missing = sorted(expected - provided)
            extra = sorted(provided - expected)
            raise IntegrationProtocolError(
                f"handlers must match config.modules exactly; "
                f"missing={missing!r} extra={extra!r}"
            )
        self._handlers = handlers
        self._initialized = False
        self._negotiated_version = None

    def _require_enabled(self) -> None:
        self._config.mas.requires(MCP_SERVER_INTEGRATION_NAME)

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one JSON-RPC message; notifications return ``None``."""
        self._require_enabled()
        if not isinstance(message, dict):
            raise IntegrationProtocolError("JSON-RPC message must be an object")
        if message.get("jsonrpc") != "2.0":
            raise IntegrationProtocolError("jsonrpc must be '2.0'")
        method = message.get("method")
        if not isinstance(method, str) or method == "":
            raise IntegrationProtocolError("method must be a non-empty string")
        has_id = "id" in message
        msg_id = message["id"] if has_id else None
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise IntegrationProtocolError("params must be an object when present")

        if method == "notifications/initialized":
            if has_id:
                raise IntegrationProtocolError("notifications/initialized must not have id")
            if not self._initialized:
                raise IntegrationProtocolError("notifications/initialized before initialize")
            return None

        if not has_id:
            raise IntegrationProtocolError(f"request method {method!r} requires id")

        error_reply = None
        result = None
        try:
            result = await self._dispatch(method, params)
        except IntegrationDisabled:
            raise
        except IntegrationProtocolError as exc:
            error_reply = _rpc_error(
                msg_id, code=_JSONRPC_INVALID_PARAMS, message=str(exc)
            )
        except Exception as exc:  # audit-ok[broad_except]: map any ModuleHandler failure to JSON-RPC -32000
            error_reply = _rpc_error(
                msg_id,
                code=_JSONRPC_SERVER_ERROR,
                message=f"handler_error:{type(exc).__name__}",
            )
        if error_reply is not None:
            return error_reply
        if result is None:
            raise RuntimeError("MCP dispatch returned no result")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if not self._initialized:
            raise IntegrationProtocolError("server not initialized")
        if method == "tools/list":
            return self._list_tools(params)
        if method == "tools/call":
            return await self._call_tool(params)
        raise IntegrationProtocolError(f"method_not_found:{method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        version = params.get("protocolVersion")
        if not isinstance(version, str) or version == "":
            raise IntegrationProtocolError(
                "initialize.protocolVersion must be a non-empty string"
            )
        if version not in self._config.supported_protocol_versions:
            raise IntegrationProtocolError(
                f"protocolVersion {version!r} not in supported_protocol_versions"
            )
        self._negotiated_version = version
        self._initialized = True
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": self._config.server_name,
                "version": self._config.server_version,
            },
        }

    def _list_tools(self, params: dict[str, Any]) -> dict[str, Any]:
        if "cursor" in params and params["cursor"] is not None:
            # Pagination not configured — refuse rather than invent pages.
            raise IntegrationProtocolError(
                "tools/list cursor not supported by this facade"
            )
        tools = [
            {
                "name": mod.name,
                "description": mod.description,
                "inputSchema": mod.input_schema,
            }
            for mod in self._config.modules
        ]
        return {"tools": tools}

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name.strip() == "":
            raise IntegrationProtocolError("tools/call name must be a non-empty string")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise IntegrationProtocolError("tools/call arguments must be an object")
        handler = self._handlers.get(name)
        if handler is None:
            raise IntegrationProtocolError(f"unknown_tool:{name}")
        data = await handler.invoke(arguments)
        if not isinstance(data, dict):
            raise IntegrationProtocolError("ModuleHandler.invoke must return a dict")
        return {
            "content": [{"type": "text", "text": _stable_text(data)}],
            "structuredContent": data,
            "isError": False,
        }


def _rpc_error(msg_id: Any, *, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }


def _stable_text(data: dict[str, Any]) -> str:
    """Deterministic text content for MCP content[].text (sorted keys)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
