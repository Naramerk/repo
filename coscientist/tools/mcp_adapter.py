"""MCP tools as ``Tool`` instances for ``invoke_tool`` (DESIGN §6.0 / A7).

Agents must not import ``coscientist.mcp``; they call these tools via
``invoke_tool`` only. Timeouts and servers come from ``McpClientConfig``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from coscientist.core.events.clock import Clock
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.mcp.client import McpClient
from coscientist.mcp.errors import McpError, McpTimeoutError, McpTransportError
from coscientist.mcp.session import McpSession
from coscientist.mcp.types import McpTool
from coscientist.tools.contract import CallCtx, ToolError, ToolOutput, ToolSpec

ToolEffectName = Literal["read", "write", "external_effect"]


class McpJournalHooks:
    """Optional journal callbacks injected by the agent layer (keeps tools ↛ agents)."""

    def __init__(
        self,
        *,
        record_request: Callable[..., Any],
        record_response: Callable[..., Any],
        put_json_artifact: Callable[..., Any],
        digest_of: Callable[[dict[str, Any]], str],
    ) -> None:
        self.record_request = record_request
        self.record_response = record_response
        self.put_json_artifact = put_json_artifact
        self.digest_of = digest_of


class McpBoundTool:
    """One MCP server tool bound for ``invoke_tool`` (provider=mcp)."""

    def __init__(
        self,
        *,
        session: McpSession,
        server_name: str,
        mcp_tool: McpTool,
        clock: Clock,
        monotonic: Callable[[], float],
        writer: StudyWriter | None = None,
        artifacts: LocalArtifactStore | None = None,
        journal: McpJournalHooks | None = None,
        on_event: Callable[[Any], None] | None = None,
        timeout_s: float,
        effect: ToolEffectName,
        is_fetch: bool,
        idempotent: bool,
    ) -> None:
        if timeout_s <= 0.0:
            raise ValueError("timeout_s must be > 0")
        self._session = session
        self._server_name = server_name
        self._clock = clock
        self._monotonic = monotonic
        self._writer = writer
        self._artifacts = artifacts
        self._journal = journal
        self._on_event = on_event
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
        request_id = f"{ctx.attempt_id}:mcp:{ctx.call_seq}:{self.spec.name}"
        args_dig = ""
        if self._journal is not None:
            args_dig = self._journal.digest_of(args)
        t0 = self._monotonic()
        if self._journal is not None and self._writer is not None:
            await self._journal.record_request(
                self._writer,
                study_id=ctx.study_id,
                request_id=request_id,
                attempt_id=ctx.attempt_id,
                server_name=self._server_name,
                method="tools/call",
                tool_name=self.spec.name,
                args_digest=args_dig,
                ts=self._clock.now(),
                on_event=self._on_event,
            )
        try:
            reply = await self._session.call_tool(self.spec.name, args)
        except McpTimeoutError as exc:
            raise ToolError("transport_sent", str(exc), retryable=False) from exc
        except McpTransportError as exc:
            retryable = bool(getattr(exc, "retryable", False))
            kind = "transport_unsent" if retryable else "transport_sent"
            raise ToolError(kind, str(exc), retryable=retryable) from exc
        except McpError as exc:
            raise ToolError("opaque", str(exc), retryable=False) from exc

        latency_ms = (self._monotonic() - t0) * 1000.0
        file_digests = [
            {"key": f.key, "digest": f.digest, "source_uri": f.source_uri}
            for f in reply.files
        ]
        if self._journal is not None and self._artifacts is not None and self._writer is not None:
            result_payload = {
                "result": reply.result,
                "content_digest": reply.content_digest,
                "is_error": reply.is_error,
                "files": file_digests,
            }
            result_art_id = await self._journal.put_json_artifact(
                self._artifacts, result_payload, tmp_name=f"mcp:{request_id}"
            )
            await self._journal.record_response(
                self._writer,
                study_id=ctx.study_id,
                request_id=request_id,
                attempt_id=ctx.attempt_id,
                server_name=self._server_name,
                method="tools/call",
                content_digest=reply.content_digest,
                result_artifact_id=result_art_id,
                is_error=reply.is_error,
                latency_ms=latency_ms,
                ts=self._clock.now(),
                on_event=self._on_event,
            )

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
                "files": file_digests,
            }
        )


async def tools_from_mcp_session(
    session: McpSession,
    *,
    server_name: str,
    clock: Clock,
    monotonic: Callable[[], float],
    call_tool_timeout_s: float,
    effect: ToolEffectName,
    is_fetch: bool,
    idempotent: bool,
    writer: StudyWriter | None = None,
    artifacts: LocalArtifactStore | None = None,
    journal: McpJournalHooks | None = None,
    on_event: Callable[[Any], None] | None = None,
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
            clock=clock,
            monotonic=monotonic,
            writer=writer,
            artifacts=artifacts,
            journal=journal,
            on_event=on_event,
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
    clock: Clock,
    monotonic: Callable[[], float],
    call_tool_timeout_s: float,
    effect: ToolEffectName,
    is_fetch: bool,
    idempotent: bool,
    writer: StudyWriter | None = None,
    artifacts: LocalArtifactStore | None = None,
    journal: McpJournalHooks | None = None,
    on_event: Callable[[Any], None] | None = None,
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
        clock=clock,
        monotonic=monotonic,
        call_tool_timeout_s=call_tool_timeout_s,
        effect=effect,
        is_fetch=is_fetch,
        idempotent=idempotent,
        writer=writer,
        artifacts=artifacts,
        journal=journal,
        on_event=on_event,
    )
    return session, tools
