"""MCP module server facade — fake handlers; no coscientist.mcp client."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.integrations.config import (
    ExternalMasConfig,
    McpPublishedModule,
    McpServerFacadeConfig,
)
from coscientist.integrations.errors import IntegrationDisabled, IntegrationProtocolError
from coscientist.integrations.mcp_server import (
    MCP_SERVER_INTEGRATION_NAME,
    McpModuleServer,
)


def _cfg(*, enabled: bool) -> McpServerFacadeConfig:
    names = (MCP_SERVER_INTEGRATION_NAME,) if enabled else ()
    return McpServerFacadeConfig(
        mas=ExternalMasConfig(enabled_integrations=names),
        protocol_version="2025-06-18",
        supported_protocol_versions=("2025-06-18",),
        server_name="coscientist-modules",
        server_version="0.1.0",
        modules=(
            McpPublishedModule(
                name="coscientist.hypotheses",
                description="propose hypotheses",
                input_schema={
                    "type": "object",
                    "properties": {"request": {"type": "string"}},
                    "required": ["request"],
                },
            ),
            McpPublishedModule(
                name="coscientist.literature",
                description="literature search",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
            McpPublishedModule(
                name="coscientist.plan",
                description="build plan",
                input_schema={
                    "type": "object",
                    "properties": {"goal": {"type": "string"}},
                    "required": ["goal"],
                },
            ),
        ),
    )


class _Handler:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def invoke(self, arguments: dict) -> dict:
        self.calls.append(arguments)
        return dict(self.payload)


def _handlers() -> dict[str, _Handler]:
    return {
        "coscientist.hypotheses": _Handler({"hypotheses": ["h1"]}),
        "coscientist.literature": _Handler({"papers": []}),
        "coscientist.plan": _Handler({"tasks": []}),
    }


@pytest.mark.asyncio
async def test_mcp_server_refuses_when_disabled() -> None:
    server = McpModuleServer(_cfg(enabled=False), handlers=_handlers())
    with pytest.raises(IntegrationDisabled) as ei:
        await server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert ei.value.name == MCP_SERVER_INTEGRATION_NAME


@pytest.mark.asyncio
async def test_initialize_list_and_call_tools() -> None:
    handlers = _handlers()
    server = McpModuleServer(_cfg(enabled=True), handlers=handlers)
    init = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    assert init is not None
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "coscientist-modules"

    note = await server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note is None

    listed = await server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert listed is not None
    names = [t["name"] for t in listed["result"]["tools"]]
    assert names == [
        "coscientist.hypotheses",
        "coscientist.literature",
        "coscientist.plan",
    ]

    called = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "coscientist.hypotheses",
                "arguments": {"request": "kinase"},
            },
        }
    )
    assert called is not None
    assert called["result"]["isError"] is False
    assert called["result"]["structuredContent"] == {"hypotheses": ["h1"]}
    assert handlers["coscientist.hypotheses"].calls == [{"request": "kinase"}]


@pytest.mark.asyncio
async def test_tools_call_before_initialize_errors() -> None:
    server = McpModuleServer(_cfg(enabled=True), handlers=_handlers())
    reply = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "coscientist.plan", "arguments": {}},
        }
    )
    assert reply is not None
    assert "error" in reply
    assert reply["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_handler_mismatch_refuses_at_construct() -> None:
    with pytest.raises(IntegrationProtocolError):
        McpModuleServer(_cfg(enabled=True), handlers={"coscientist.hypotheses": _Handler({})})


@pytest.mark.asyncio
async def test_unsupported_protocol_version() -> None:
    server = McpModuleServer(_cfg(enabled=True), handlers=_handlers())
    reply = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        }
    )
    assert reply is not None
    assert "error" in reply


def test_config_requires_modules() -> None:
    with pytest.raises(ValidationError):
        McpServerFacadeConfig(
            mas=ExternalMasConfig(enabled_integrations=(MCP_SERVER_INTEGRATION_NAME,)),
            protocol_version="2025-06-18",
            supported_protocol_versions=("2025-06-18",),
            server_name="x",
            server_version="1",
            modules=(),
        )


@pytest.mark.asyncio
async def test_handler_exception_maps_to_server_error() -> None:
    class Boom:
        async def invoke(self, arguments: dict) -> dict:
            raise RuntimeError("boom")

    handlers = _handlers()
    handlers["coscientist.plan"] = Boom()  # type: ignore[assignment]
    server = McpModuleServer(_cfg(enabled=True), handlers=handlers)
    await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    await server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    reply = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "coscientist.plan", "arguments": {"goal": "g"}},
        }
    )
    assert reply is not None
    assert reply["error"]["code"] == -32000
    assert "RuntimeError" in reply["error"]["message"]
