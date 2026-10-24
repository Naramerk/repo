"""MCP facade wiring: publish modules that call the study API (DESIGN §6.5).

Re-exports ``McpModuleServer``; API-layer handlers implement ``ModuleHandler``.
"""

from __future__ import annotations

from typing import Any

from coscientist.integrations.config import McpServerFacadeConfig
from coscientist.integrations.mcp_server import (
    MCP_SERVER_INTEGRATION_NAME,
    McpModuleServer,
)
from coscientist.integrations.ports import ModuleHandler

__all__ = [
    "MCP_SERVER_INTEGRATION_NAME",
    "McpModuleServer",
    "build_mcp_module_server",
]


def build_mcp_module_server(
    config: McpServerFacadeConfig,
    *,
    handlers: dict[str, ModuleHandler],
) -> McpModuleServer:
    """Build the MCP module server (handlers must match ``config.modules``)."""
    return McpModuleServer(config, handlers=handlers)


class CallableModuleHandler:
    """Adapter: async callable ``(arguments) -> dict`` as ``ModuleHandler``."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._fn(arguments)
        if not isinstance(result, dict):
            raise TypeError("module handler must return dict")
        return result
