"""Session recovery tests: closed/expired streamable-HTTP session, no network.

Wave-7 live evidence: one slow (timed-out) ``papers-search`` call closed the
transport session; every later call on that session failed with
"streamable HTTP session is closed" (retryable=False) and the whole literature
stage died. These tests pin the recovery contract:

- a closed session is transport state: the session layer re-opens + re-runs
  the initialize handshake, whatever tool hit the closed session;
- the failed call is retried once only when the caller vouches the call is
  re-issuable (``retry_after_recovery=True`` for idempotent tools);
- non-idempotent calls fail once and the NEXT call rides the fresh session;
- server-side expiry (404 on a session id) recovers through the same path;
- recovery is bounded: consecutive recoveries without a successful call hit
  a budget and refuse (no recover/fail loop).
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from coscientist.mcp import (
    McpClientConfig,
    McpSessionClosedError,
    McpTimeoutError,
    McpTransportError,
    StreamableHttpServerConfig,
)
from coscientist.mcp.session import _MAX_SESSION_RECOVERIES
from tests.mcp.conftest import _BufStream, make_client, make_config

_CALL_RESULT = {"content": [{"type": "text", "text": "ok"}], "isError": False}


class _ScriptedHttp:
    """HttpTransport fake: dynamic initialize answers, scripted tools/call.

    ``initialize`` always succeeds with a fresh id-echoed result and a new
    session id (``sess-1``, ``sess-2``, ...) — exactly what a live server does
    on a fresh handshake. ``tools/call`` outcomes come from the scripted list:
    ``Exception`` (raised from ``open`` — a TimeoutError becomes
    ``McpTimeoutError``), a raw ``_BufStream`` (e.g. a 404), or a result dict
    (wrapped as a 200 JSON-RPC result echoing the request id). Notifications
    get 202; DELETE gets 200.
    """

    def __init__(self, call_outcomes: list[Any]) -> None:
        self._call_outcomes = list(call_outcomes)
        self.calls: list[dict[str, Any]] = []
        self.init_count = 0

    async def open(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_s: float,
        connect_ip: str | None,
        server_hostname: str | None = None,
    ) -> Any:
        self.calls.append({"method": method, "headers": dict(headers), "body": body})
        if method == "DELETE":
            return _BufStream(200, {}, b"")
        payload: dict[str, Any] = json.loads(body) if body else {}
        rpc_method = payload.get("method")
        if rpc_method == "initialize":
            self.init_count += 1
            body_out = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "serverInfo": {"name": "r", "version": "1"},
                    },
                }
            ).encode()
            return _BufStream(
                200,
                {"content-type": "application/json", "mcp-session-id": f"sess-{self.init_count}"},
                body_out,
            )
        if rpc_method == "tools/call" and "id" in payload:
            item = self._call_outcomes.pop(0) if self._call_outcomes else _BufStream(202, {}, b"")
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, _BufStream):
                return item
            return _BufStream(
                200,
                {"content-type": "application/json"},
                json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": item}).encode(),
            )
        return _BufStream(202, {}, b"")


def _http_config(http: _ScriptedHttp) -> McpClientConfig:
    return make_config(
        servers=(
            StreamableHttpServerConfig(
                transport="streamable_http",
                name="remote",
                url="https://mcp.example/mcp",
                api_key_env=None,
            ),
        )
    )


async def _connect(http: _ScriptedHttp):
    client = make_client(_http_config(http), http_transport=http)
    return await client.connect("remote")


def _session_id(calls: list[dict[str, Any]], index: int) -> str | None:
    return calls[index]["headers"].get("Mcp-Session-Id")


@pytest.mark.asyncio
async def test_closed_session_recovers_and_idempotent_call_succeeds() -> None:
    """timeout → session closed → next idempotent call: re-init + retry succeeds."""
    http = _ScriptedHttp([TimeoutError(), _CALL_RESULT])
    session = await _connect(http)

    with pytest.raises(McpTimeoutError):
        await session.call_tool("t", {}, retry_after_recovery=True)

    reply = await session.call_tool("t", {}, retry_after_recovery=True)
    assert reply.is_error is False
    # calls: init1, notify, call(timeout), cancelled, DELETE, init2, notify, call(ok)
    assert len(http.calls) == 8
    assert _session_id(http.calls, 4) == "sess-1"  # DELETE rode the old session
    assert "Mcp-Session-Id" not in http.calls[5]["headers"]  # re-init: no session id
    assert _session_id(http.calls, 7) == "sess-2"  # retried call on the fresh session
    assert http.init_count == 2
    # The successful retried call resets the recovery budget.
    assert session._session_recoveries == 0


@pytest.mark.asyncio
async def test_closed_session_non_idempotent_fails_once_then_next_call_succeeds() -> None:
    """Non-idempotent call: session recovered, call fails once, next call succeeds."""
    http = _ScriptedHttp([TimeoutError(), _CALL_RESULT])
    session = await _connect(http)

    with pytest.raises(McpTimeoutError):
        await session.call_tool("t", {})

    # No auto-retry for a call the caller did not vouch for — but the session
    # itself is recovered underneath (fresh handshake happened).
    with pytest.raises(McpSessionClosedError):
        await session.call_tool("t", {})
    assert http.init_count == 2

    # The next call rides the fresh session.
    reply = await session.call_tool("t", {})
    assert reply.is_error is False
    assert _session_id(http.calls, len(http.calls) - 1) == "sess-2"


@pytest.mark.asyncio
async def test_http_404_session_expiry_recovers_and_gates_retry() -> None:
    """Spec 2025-06-18: 404 on a session id → same recovery, same retry gate."""
    http = _ScriptedHttp([_BufStream(404, {}, b""), _CALL_RESULT])
    session = await _connect(http)

    # Non-idempotent: recover, then fail once with the honest status error.
    with pytest.raises(McpTransportError, match="status 404"):
        await session.call_tool("t", {})
    assert http.init_count == 2

    reply = await session.call_tool("t", {})
    assert reply.is_error is False
    assert _session_id(http.calls, len(http.calls) - 1) == "sess-2"


@pytest.mark.asyncio
async def test_recovery_budget_bounds_consecutive_recoveries() -> None:
    """Every call fails again: recoveries stop at the budget, no loop."""
    http = _ScriptedHttp([TimeoutError()] * 64)
    session = await _connect(http)

    with pytest.raises(McpTimeoutError):
        await session.call_tool("t", {}, retry_after_recovery=True)
    for _ in range(_MAX_SESSION_RECOVERIES):
        # Each call: closed session → one recovery → retry times out again.
        with pytest.raises(McpTimeoutError):
            await session.call_tool("t", {}, retry_after_recovery=True)
    assert http.init_count == 1 + _MAX_SESSION_RECOVERIES
    assert session._session_recoveries == _MAX_SESSION_RECOVERIES

    # Budget exhausted: the closed-session error becomes a plain transport
    # refusal and NO further initialize is attempted.
    with pytest.raises(McpTransportError, match="budget exhausted"):
        await session.call_tool("t", {}, retry_after_recovery=True)
    assert http.init_count == 1 + _MAX_SESSION_RECOVERIES


@pytest.mark.asyncio
async def test_bound_tool_timeout_recovers_and_succeeds_one_receipt() -> None:
    """Composed production scenario: adapter timeout retry rides session recovery.

    One bound-tool call (one recorded invocation) survives a slow first
    attempt: the adapter's idempotent timeout retry hits the closed session,
    the session layer recovers, and the second attempt succeeds.
    """
    from coscientist.core.execution.domain import HitlPolicy
    from coscientist.mcp.types import McpTool
    from coscientist.tools.contract import CallCtx
    from coscientist.tools.mcp_adapter import McpBoundTool, McpToolProfile

    http = _ScriptedHttp([TimeoutError(), _CALL_RESULT])
    session = await _connect(http)
    tool = McpBoundTool(
        session=session,
        server_name="remote",
        mcp_tool=McpTool(name="t", description=None, input_schema={"type": "object"}),
        profile=McpToolProfile(effect="read", is_fetch=False, idempotent=True, max_retries=1),
    )
    ctx = CallCtx(study_id="st", task_key="T0", attempt_id="att#1", hitl_policy=HitlPolicy())

    output = await tool.call({}, ctx)
    assert output.data["result"] == _CALL_RESULT
    assert http.init_count == 2  # timeout recovery happened inside this one call
    assert session._session_recoveries == 0
