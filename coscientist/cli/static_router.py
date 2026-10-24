"""Hermetic ``ToolRouter`` subclass for CLI cassettes (fixed payloads, no MCP I/O).

Subclass of the concrete MCP ``ToolRouter`` (#139) — not a parallel Protocol.
Production MCP wiring uses ``ToolRouter.open`` / ``attach_session``; this path only
serves ``--tools-json`` study cassettes until CLI grows a real MCP config flag.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coscientist.tools.contract import CallCtx, ToolOutput
from coscientist.tools.mcp_adapter import McpToolProfile
from coscientist.tools.router import McpServerRoute, Resolution, ToolNeed, ToolRouter, ToolRouterConfig

_STATIC_SERVER = "static"


class StaticToolRouter(ToolRouter):
    """``ToolRouter`` with fixed name→payload map; non-empty ``args`` refused."""

    def __init__(self, *, tools: Mapping[str, Mapping[str, object]]) -> None:
        if not tools:
            raise ValueError("StaticToolRouter tools must be non-empty")
        for name in tools:
            if type(name) is not str or not name.strip():
                raise ValueError("StaticToolRouter tool names must be non-empty str")
            if not isinstance(tools[name], Mapping):
                raise ValueError(f"StaticToolRouter tools[{name!r}] must be a mapping")
        profiles = {
            name: McpToolProfile(effect="read", is_fetch=False, idempotent=True, max_retries=0)
            for name in tools
        }
        super().__init__(
            ToolRouterConfig(
                servers=(McpServerRoute(server_name=_STATIC_SERVER, tools=profiles),)
            )
        )
        self._payloads = {name: dict(payload) for name, payload in tools.items()}
        # Cassette is "open" without MCP sessions; resolve/call are overridden.
        self._opened = True

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._payloads)

    def resolve(self, need: ToolNeed) -> Resolution:
        missing = Resolution(status="tool_missing", server_name=None, tool_name=None)
        if need.kind != "by_name":
            return missing
        value = need.value
        prefix = f"{_STATIC_SERVER}:"
        if value.startswith(prefix):
            tool_name = value[len(prefix) :]
        else:
            tool_name = value
        if tool_name in self._payloads:
            return Resolution(
                status="tool_found",
                server_name=_STATIC_SERVER,
                tool_name=tool_name,
            )
        return missing

    async def call(
        self,
        server_name: str,
        tool_name: str,
        args: dict[str, Any],
        ctx: CallCtx,
    ) -> ToolOutput:
        del ctx
        if server_name != _STATIC_SERVER:
            raise KeyError(f"unknown server {server_name!r}")
        if args:
            raise ValueError(
                f"StaticToolRouter refuses non-empty args for {tool_name!r} "
                f"(server={server_name!r}); static payloads are not parameterized"
            )
        if tool_name not in self._payloads:
            raise KeyError(tool_name)
        return ToolOutput(data=dict(self._payloads[tool_name]))
