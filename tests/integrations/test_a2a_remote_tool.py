"""Outbound A2A remote agent as Tool — fake transport only."""

from __future__ import annotations

import json

import pytest

from coscientist.core.execution.domain import HitlPolicy
from coscientist.integrations.config import A2ARemoteToolConfig, ExternalMasConfig
from coscientist.integrations.errors import IntegrationTransportError
from coscientist.integrations.ports import A2ATransportResponse
from coscientist.tools.contract import CallCtx, ToolError
from coscientist.tools.integrations.a2a_remote import (
    A2A_REMOTE_INTEGRATION_NAME,
    RemoteA2AAgentTool,
)


def _cfg(*, enabled: bool) -> A2ARemoteToolConfig:
    names = (A2A_REMOTE_INTEGRATION_NAME,) if enabled else ()
    return A2ARemoteToolConfig(
        mas=ExternalMasConfig(enabled_integrations=names),
        tool_name="remote_a2a_agent",
        base_url="https://peer.example/a2a/",
        create_task_path="tasks",
        timeout_s=5.0,
        ok_status_codes=(200,),
        request_content_type="application/json",
        message_field="message",
        result_field="result",
    )


def _ctx() -> CallCtx:
    return CallCtx(
        study_id="st1",
        task_key="t1",
        attempt_id="a1",
        hitl_policy=HitlPolicy(),
    )


class _FakeTransport:
    def __init__(self, response: A2ATransportResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_s: float,
    ) -> A2ATransportResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_s": timeout_s,
            }
        )
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.mark.asyncio
async def test_remote_tool_refuses_when_disabled() -> None:
    tool = RemoteA2AAgentTool(_cfg(enabled=False), transport=_FakeTransport(
        A2ATransportResponse(status_code=200, headers={}, body=b"{}")
    ))
    with pytest.raises(ToolError) as ei:
        await tool.call({"message": "hello"}, _ctx())
    assert ei.value.kind == "local"
    assert "integration_disabled" in ei.value.detail


@pytest.mark.asyncio
async def test_remote_tool_posts_and_returns_result() -> None:
    body = json.dumps({"result": {"ok": True}}, separators=(",", ":")).encode()
    transport = _FakeTransport(
        A2ATransportResponse(status_code=200, headers={}, body=body)
    )
    tool = RemoteA2AAgentTool(_cfg(enabled=True), transport=transport)
    assert tool.spec.provider == "http"
    out = await tool.call({"message": "run analysis"}, _ctx())
    assert out.data == {"result": {"ok": True}}
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://peer.example/a2a/tasks"
    assert json.loads(call["body"]) == {"message": "run analysis"}


@pytest.mark.asyncio
async def test_remote_tool_maps_transport_error() -> None:
    transport = _FakeTransport(
        IntegrationTransportError("conn reset", retryable=True)
    )
    tool = RemoteA2AAgentTool(_cfg(enabled=True), transport=transport)
    with pytest.raises(ToolError) as ei:
        await tool.call({"message": "x"}, _ctx())
    assert ei.value.kind == "transport_unsent"
    assert ei.value.retryable is True


@pytest.mark.asyncio
async def test_remote_tool_rejects_non_ok_status() -> None:
    transport = _FakeTransport(
        A2ATransportResponse(status_code=503, headers={}, body=b"{}")
    )
    tool = RemoteA2AAgentTool(_cfg(enabled=True), transport=transport)
    with pytest.raises(ToolError) as ei:
        await tool.call({"message": "x"}, _ctx())
    assert ei.value.kind == "transport_sent"
    assert "a2a_http_status:503" in ei.value.detail


@pytest.mark.asyncio
async def test_remote_tool_requires_message() -> None:
    tool = RemoteA2AAgentTool(
        _cfg(enabled=True),
        transport=_FakeTransport(
            A2ATransportResponse(status_code=200, headers={}, body=b"{}")
        ),
    )
    with pytest.raises(ToolError) as ei:
        await tool.call({}, _ctx())
    assert ei.value.kind == "local"
