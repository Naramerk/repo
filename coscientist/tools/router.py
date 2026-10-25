"""MCP ToolRouter: route calls to ``coscientist.mcp`` via ``mcp_adapter`` (DESIGN §6.3).

MCP-only path for M1 task B. Alembic/coder fallback and ``ToolResolved`` journal
are out of scope (tool-gap G3/G6). Agents still call tools through ``invoke_tool``;
this router binds and dispatches to ``McpBoundTool``.

Every server and tool must be listed in ``ToolRouterConfig`` — no discovery, no
silent defaults.

Capability #8 surface: ``ToolNeed`` / ``Resolution`` / ``ToolRouter.resolve``
(by_name against bound MCP tools; by_query / alembic / coder → tool_missing until G3/G6).
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coscientist.core.execution.call_ctx import CallCtx
from coscientist.mcp.client import McpClient
from coscientist.mcp.errors import McpError
from coscientist.mcp.session import McpSession
from coscientist.tools.contract import ToolOutput
from coscientist.tools.mcp_adapter import (
    McpAdapterError,
    McpBoundTool,
    McpToolKey,
    McpToolProfile,
    bind_mcp_server_tools,
    mcp_tool_key,
    tools_from_mcp_session,
)
from coscientist.tools.tool_resolution import ToolResolutionStatus

__all__ = [
    "McpServerRoute",
    "Resolution",
    "ToolNeed",
    "ToolRouter",
    "ToolRouterCloseError",
    "ToolRouterConfig",
    "ToolRouterError",
]


class ToolNeed(BaseModel):
    """What the agent needs resolved before a tool call (DESIGN §6.3 / CAP #8)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["by_name", "by_query"]
    value: str = Field(min_length=1)


class Resolution(BaseModel):
    """Result of ``ToolRouter.resolve`` (capability #8 statuses)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    status: ToolResolutionStatus
    server_name: str | None
    tool_name: str | None

    @model_validator(mode="after")
    def _status_matches_fields(self) -> Self:
        if self.status == "tool_found":
            if self.server_name is None or not self.server_name:
                raise ValueError("tool_found requires non-empty server_name")
            if self.tool_name is None or not self.tool_name:
                raise ValueError("tool_found requires non-empty tool_name")
        elif self.server_name is not None or self.tool_name is not None:
            raise ValueError(f"{self.status} requires server_name=tool_name=None")
        return self


class ToolRouterError(Exception):
    """Fail-closed router error (unknown server/tool, not open, bind failure)."""


class ToolRouterCloseError(ToolRouterError):
    """Session close failure(s); ``causes`` is required and non-empty."""

    def __init__(self, message: str, *, causes: tuple[BaseException, ...]) -> None:
        if not causes:
            raise ValueError("ToolRouterCloseError.causes must be non-empty")
        super().__init__(message)
        self.causes = causes


def _require_id(value: str, *, label: str) -> str:
    if not value:
        raise ValueError(f"{label} must be non-empty")
    if any(ch.isspace() for ch in value):
        raise ValueError(f"{label} must not contain whitespace: {value!r}")
    return value


async def _aclose_sessions(sessions: list[McpSession]) -> list[Exception]:
    """Close every session; collect ``McpError``/``OSError``/``TimeoutError`` (no early abort)."""
    errors: list[Exception] = []
    for session in sessions:
        try:
            await session.aclose()
        except (McpError, OSError, TimeoutError) as exc:
            errors.append(exc)
    return errors


def _raise_close_errors(errors: list[Exception]) -> None:
    if not errors:
        return
    causes = tuple(errors)
    detail = f"session close failed ({len(causes)}): " + "; ".join(repr(e) for e in causes)
    err = ToolRouterCloseError(detail, causes=causes)
    if len(causes) == 1:
        raise err from causes[0]
    raise err from ExceptionGroup("session close failures", list(causes))


class McpServerRoute(BaseModel):
    """Declared tools for one MCP server (exact names; fail-closed bindings)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    server_name: str = Field(min_length=1)
    tools: dict[str, McpToolProfile] = Field(min_length=1)

    @field_validator("server_name")
    @classmethod
    def _server_id(cls, v: str) -> str:
        return _require_id(v, label="server_name")

    @field_validator("tools")
    @classmethod
    def _tool_ids(cls, v: dict[str, McpToolProfile]) -> dict[str, McpToolProfile]:
        for name in v:
            _require_id(name, label="tool name")
        return v


class ToolRouterConfig(BaseModel):
    """Explicit MCP routes. Servers are never discovered."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    servers: tuple[McpServerRoute, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_servers(self) -> ToolRouterConfig:
        names = [s.server_name for s in self.servers]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate server_name in ToolRouterConfig: {names}")
        return self


class ToolRouter:
    """Route ``(server, tool)`` calls through ``mcp_adapter`` to ``McpClient``."""

    def __init__(self, config: ToolRouterConfig) -> None:
        self._config = config
        self._tools: dict[McpToolKey, McpBoundTool] = {}
        self._sessions: list[McpSession] = []
        self._opened = False

    @property
    def config(self) -> ToolRouterConfig:
        return self._config

    def _route(self, server_name: str) -> McpServerRoute:
        for route in self._config.servers:
            if route.server_name == server_name:
                return route
        raise ToolRouterError(
            f"unknown server {server_name!r}; configured={sorted(s.server_name for s in self._config.servers)}"
        )

    def _sync_opened(self) -> None:
        configured = {s.server_name for s in self._config.servers}
        bound_servers = {key[0] for key in self._tools}
        self._opened = configured <= bound_servers

    async def open(self, client: McpClient) -> None:
        """Connect every configured server via ``bind_mcp_server_tools`` (real transport)."""
        if self._tools or self._sessions:
            raise ToolRouterError("ToolRouter already has bindings")
        opened: list[McpSession] = []
        bound: dict[McpToolKey, McpBoundTool] = {}
        committed = False
        try:
            for route in self._config.servers:
                try:
                    session, tools = await bind_mcp_server_tools(
                        client,
                        route.server_name,
                        bindings=dict(route.tools),
                    )
                except (McpAdapterError, McpError) as exc:
                    raise ToolRouterError(f"failed to bind server {route.server_name!r}: {exc}") from exc
                opened.append(session)
                for key, tool in tools.items():
                    if key in bound:
                        raise ToolRouterError(f"duplicate MCP tool key {key!r}")
                    bound[key] = tool
            self._sessions = opened
            self._tools = bound
            self._sync_opened()
            committed = True
        finally:
            if not committed:
                # Close all; do not raise close errors (would mask bind/OSError/TimeoutError).
                await _aclose_sessions(opened)

    async def attach_session(self, session: McpSession, *, server_name: str) -> None:
        """Bind one already-open session (tests: FakeStdioServer / ``connect_memory``)."""
        route = self._route(server_name)
        try:
            tools = await tools_from_mcp_session(
                session,
                server_name=server_name,
                bindings=dict(route.tools),
            )
        except (McpAdapterError, McpError) as exc:
            raise ToolRouterError(str(exc)) from exc
        for key in tools:
            if key in self._tools:
                raise ToolRouterError(f"duplicate MCP tool key {key!r}")
        self._tools.update(tools)
        self._sessions.append(session)
        self._sync_opened()

    async def aclose(self) -> None:
        sessions = self._sessions
        self._sessions = []
        self._tools = {}
        self._opened = False
        _raise_close_errors(await _aclose_sessions(sessions))

    def require_tool(self, server_name: str, tool_name: str) -> McpBoundTool:
        """Fail-closed lookup of a bound tool."""
        if not self._opened:
            raise ToolRouterError("ToolRouter is not open")
        self._route(server_name)  # unknown server even if somehow bound
        key = mcp_tool_key(server_name, tool_name)
        try:
            return self._tools[key]
        except KeyError as exc:
            known = sorted(f"{s}:{t}" for s, t in self._tools)
            raise ToolRouterError(f"unknown tool {server_name}:{tool_name}; bound={known}") from exc

    def resolve(self, need: ToolNeed) -> Resolution:
        """Map ``ToolNeed`` to a bound MCP tool (CAP #8).

        ``by_name`` accepts only exact ``server:tool`` (``ToolSpec.name``). Bare names
        and multi-colon values → ``tool_missing``. ``by_query`` / alembic / coder →
        ``tool_missing``. Not open → ``ToolRouterError`` (same as ``call`` / ``require_tool``).
        """
        if not self._opened:
            raise ToolRouterError("ToolRouter is not open")
        missing = Resolution(status="tool_missing", server_name=None, tool_name=None)
        if need.kind != "by_name":
            return missing
        value = need.value
        # Exact ``ToolSpec.name`` match (``mcp_tool_spec_name`` = server:tool); no colon parse.
        for server_name, tool_name in self._tools:
            bound = self._tools[(server_name, tool_name)]
            if bound.spec.name == value:
                return Resolution(
                    status="tool_found",
                    server_name=server_name,
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
        """Dispatch to the bound ``McpBoundTool`` (MCP client via ``mcp_adapter``)."""
        return await self.require_tool(server_name, tool_name).call(args, ctx)
