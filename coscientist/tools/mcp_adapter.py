"""MCP tools as ``Tool`` instances for ``invoke_tool`` (DESIGN §6.0 / A7).

Agents must not import ``coscientist.mcp``; they call these tools via
``invoke_tool`` only. Timeouts and servers come from ``McpClientConfig``.
Effect / is_fetch / idempotent come from the profile (no name inference).
"""

from __future__ import annotations

from typing import Any, Literal

from coscientist.mcp.client import McpClient
from coscientist.mcp.errors import McpError, McpTimeoutError, McpTransportError
from coscientist.mcp.session import McpSession
from coscientist.mcp.types import McpTool
from coscientist.tools.contract import CallCtx, ToolError, ToolOutput, ToolSpec

ToolEffectName = Literal["read", "write", "external_effect"]


class McpBoundTool:
    """One MCP server tool bound for ``invoke_tool`` (provider=mcp)."""

    def __init__(
        self,
        *,
        session: McpSession,
        server_name: str,
        mcp_tool: McpTool,
        timeout_s: float,
        effect: ToolEffectName,
        is_fetch: bool,
        idempotent: bool,
    ) -> None:
        if timeout_s <= 0.0:
            raise ValueError("timeout_s must be > 0")
        self._session = session
        self.server_name = server_name
        self.spec = ToolSpec(
            name=mcp_tool.name,
            provider="mcp",
            input_schema=dict(mcp_tool.input_schema),
            effect=effect,
            is_fetch=is_fetch,
            idempotent=idempotent,
            timeout_s=timeout_s,
            max_retries=0,
        )
        self.description = mcp_tool.description

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
        _ = ctx
        try:
            reply = await self._session.call_tool(self.spec.name, args)
        except McpTimeoutError as exc:
            raise ToolError("transport_sent", str(exc), retryable=False) from exc
        except McpTransportError as exc:
            kind = "transport_unsent" if exc.retryable else "transport_sent"
            raise ToolError(kind, str(exc), retryable=exc.retryable) from exc
        except McpError as exc:
            raise ToolError("opaque", str(exc), retryable=False) from exc

        if reply.is_error:
            raise ToolError(
                "opaque",
                f"MCP tool {self.spec.name!r} returned isError",
                retryable=False,
            )
        return ToolOutput(
            data={
                "result": reply.result,
                "content_digest": reply.content_digest,
                "files": [
                    {"key": f.key, "digest": f.digest, "source_uri": f.source_uri}
                    for f in reply.files
                ],
            }
        )


async def tools_from_mcp_session(
    session: McpSession,
    *,
    server_name: str,
    call_tool_timeout_s: float,
    effect: ToolEffectName,
    is_fetch: bool,
    idempotent: bool,
) -> list[McpBoundTool]:
    """List tools on an open session and wrap them (profile supplies effect flags)."""
    if call_tool_timeout_s <= 0.0:
        raise ValueError("call_tool_timeout_s must be > 0")
    listed = await session.list_tools()
    return [
        McpBoundTool(
            session=session,
            server_name=server_name,
            mcp_tool=tool,
            timeout_s=call_tool_timeout_s,
            effect=effect,
            is_fetch=is_fetch,
            idempotent=idempotent,
        )
        for tool in listed.tools
    ]


async def bind_mcp_server_tools(
    client: McpClient,
    server_name: str,
    *,
    call_tool_timeout_s: float,
    effect: ToolEffectName,
    is_fetch: bool,
    idempotent: bool,
) -> tuple[McpSession, list[McpBoundTool]]:
    """Connect, list tools, return session + bound ``Tool`` wrappers.

    ``effect`` / ``is_fetch`` / ``idempotent`` must come from the profile for
    the server (no inference from tool names). Callers register matching
    ``RegisteredToolSpec`` rows before ``invoke_tool``.
    """
    session = await client.connect(server_name)
    tools = await tools_from_mcp_session(
        session,
        server_name=server_name,
        call_tool_timeout_s=call_tool_timeout_s,
        effect=effect,
        is_fetch=is_fetch,
        idempotent=idempotent,
    )
    return session, tools
