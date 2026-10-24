"""MCP tools as ``Tool`` instances for ``invoke_tool`` (DESIGN §6.0 / A7).

Agents must not import ``coscientist.mcp``; they call these tools via
``invoke_tool`` only. RPC timeout is ``McpSession.call_tool_timeout_s`` only
(same as ``McpClientConfig.call_tool_timeout_s``). Effect / is_fetch /
idempotent / max_retries come from a per-tool profile map (no name inference).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from coscientist.mcp.client import McpClient
from coscientist.mcp.errors import McpError, McpTimeoutError, McpTransportError
from coscientist.mcp.session import McpSession
from coscientist.mcp.types import McpTool
from coscientist.tools.contract import (
    CallCtx,
    ToolEffect,
    ToolError,
    ToolErrorKind,
    ToolOutput,
    ToolSpec,
)

McpToolKey = tuple[str, str]


class McpAdapterError(Exception):
    """Fail-closed adapter configuration / binding error."""


class McpToolProfile(BaseModel):
    """Per-tool flags for one MCP server tool (profile / catalog; no defaults)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effect: ToolEffect
    is_fetch: bool
    idempotent: bool
    max_retries: int = Field(ge=0)


def mcp_tool_key(server_name: str, tool_name: str) -> McpToolKey:
    """Stable identity for a tool on a named server (collision-safe across servers)."""
    if not server_name or not tool_name:
        raise McpAdapterError("server_name and tool_name must be non-empty")
    return (server_name, tool_name)


def mcp_tool_spec_name(server_name: str, tool_name: str) -> str:
    """``ToolSpec.name`` / registry name for ``(server_name, tool_name)``."""
    key = mcp_tool_key(server_name, tool_name)
    return f"{key[0]}:{key[1]}"


class McpBoundTool:
    """One MCP server tool bound for ``invoke_tool`` (provider=mcp)."""

    def __init__(
        self,
        *,
        session: McpSession,
        server_name: str,
        mcp_tool: McpTool,
        profile: McpToolProfile,
    ) -> None:
        self._session = session
        self._mcp_name = mcp_tool.name
        self.server_name = server_name
        self.key = mcp_tool_key(server_name, mcp_tool.name)
        self.spec = ToolSpec(
            name=mcp_tool_spec_name(server_name, mcp_tool.name),
            provider="mcp",
            input_schema=dict(mcp_tool.input_schema),
            effect=profile.effect,
            is_fetch=profile.is_fetch,
            idempotent=profile.idempotent,
            timeout_s=session.call_tool_timeout_s,
            max_retries=profile.max_retries,
        )
        self.description = mcp_tool.description

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
        # Attempt/study ids are part of the call identity; wall deadline is enforced
        # by McpSession (call_tool_timeout_s) / invoke_tool — not re-derived here.
        _attempt = ctx.attempt_id
        _study = ctx.study_id
        try:
            reply = await self._session.call_tool(self._mcp_name, args)
        except McpTimeoutError as exc:
            raise ToolError("transport_sent", str(exc), retryable=False) from exc
        except McpTransportError as exc:
            kind: ToolErrorKind = (
                "transport_unsent" if exc.retryable else "transport_sent"
            )
            raise ToolError(kind, str(exc), retryable=exc.retryable) from exc
        except McpError as exc:
            raise ToolError("opaque", str(exc), retryable=False) from exc

        if reply.is_error:
            raise ToolError(
                "opaque",
                (
                    f"MCP tool {self.spec.name!r} returned isError "
                    f"(study={_study!r} attempt={_attempt!r}); "
                    f"content_digest={reply.content_digest}; result={reply.result!r}"
                ),
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


def _require_bindings(
    tools: list[McpTool],
    bindings: Mapping[str, McpToolProfile],
) -> None:
    listed = {t.name for t in tools}
    bound = set(bindings)
    missing = listed - bound
    if missing:
        raise McpAdapterError(f"missing McpToolProfile for tools: {sorted(missing)}")
    extra = bound - listed
    if extra:
        raise McpAdapterError(f"McpToolProfile for unknown tools: {sorted(extra)}")


async def tools_from_mcp_session(
    session: McpSession,
    *,
    server_name: str,
    bindings: Mapping[str, McpToolProfile],
) -> dict[McpToolKey, McpBoundTool]:
    """List tools on an open session and wrap them (keyed by ``(server, tool)``)."""
    if server_name != session.server_name:
        raise McpAdapterError(
            f"server_name {server_name!r} != session.server_name {session.server_name!r}"
        )
    listed = await session.list_tools()
    _require_bindings(list(listed.tools), bindings)
    out: dict[McpToolKey, McpBoundTool] = {}
    for tool in listed.tools:
        bound = McpBoundTool(
            session=session,
            server_name=server_name,
            mcp_tool=tool,
            profile=bindings[tool.name],
        )
        if bound.key in out:
            raise McpAdapterError(f"duplicate MCP tool key {bound.key!r}")
        out[bound.key] = bound
    return out


async def bind_mcp_server_tools(
    client: McpClient,
    server_name: str,
    *,
    bindings: Mapping[str, McpToolProfile],
) -> tuple[McpSession, dict[McpToolKey, McpBoundTool]]:
    """Connect, list tools, return session + bound tools keyed by ``(server, tool)``."""
    session = await client.connect(server_name)
    listed_ok = False
    try:
        tools = await tools_from_mcp_session(
            session, server_name=server_name, bindings=bindings
        )
        listed_ok = True
        return session, tools
    finally:
        if not listed_ok:
            await session.aclose()
