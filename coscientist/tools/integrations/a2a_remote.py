"""Outbound A2A: remote agent callable as a Tool (DESIGN §6.5).

Uses injected ``A2AHttpTransport`` (no httpx). Gated by ``a2a_remote`` in
``ExternalMasConfig.enabled_integrations``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

from coscientist.integrations.config import A2ARemoteToolConfig
from coscientist.integrations.errors import (
    IntegrationDisabled,
    IntegrationProtocolError,
    IntegrationTransportError,
)
from coscientist.integrations.ports import A2AHttpTransport
from coscientist.tools.contract import CallCtx, ToolError, ToolOutput, ToolSpec

A2A_REMOTE_INTEGRATION_NAME = "a2a_remote"


class RemoteA2AAgentTool:
    """Tool that forwards a prompt to a remote A2A agent over HTTP."""

    def __init__(
        self,
        config: A2ARemoteToolConfig,
        *,
        transport: A2AHttpTransport,
    ) -> None:
        self._config = config
        self._transport = transport
        self.spec = ToolSpec(
            name=config.tool_name,
            provider="http",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"result": {}},
                "required": ["result"],
            },
            effect="external_effect",
            is_fetch=False,
            idempotent=False,
            supports_idem_key=False,
            timeout_s=config.timeout_s,
            max_retries=0,
        )

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
        try:
            self._config.mas.requires(A2A_REMOTE_INTEGRATION_NAME)
        except IntegrationDisabled as exc:
            raise ToolError("local", str(exc), retryable=False) from exc

        message = args.get("message")
        if not isinstance(message, str) or message.strip() == "":
            raise ToolError("local", "message must be a non-empty string", retryable=False)

        url = urljoin(
            self._config.base_url.rstrip("/") + "/",
            self._config.create_task_path,
        )
        body_obj = {self._config.message_field: message}
        raw_body = json.dumps(body_obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": self._config.request_content_type,
            "Accept": self._config.request_content_type,
        }
        try:
            response = await self._transport.request(
                "POST",
                url,
                headers=headers,
                body=raw_body,
                timeout_s=self._config.timeout_s,
            )
        except IntegrationTransportError as exc:
            kind = "transport_unsent" if exc.retryable else "transport_sent"
            raise ToolError(kind, exc.detail, retryable=exc.retryable) from exc
        except Exception as exc:  # audit-ok[broad_except]: A2AHttpTransport may raise any; map to ToolError
            raise ToolError(
                "opaque",
                f"a2a_transport:{type(exc).__name__}",
                retryable=False,
            ) from exc

        if response.status_code not in self._config.ok_status_codes:
            raise ToolError(
                "transport_sent",
                f"a2a_http_status:{response.status_code}",
                retryable=False,
            )
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolError(
                "transport_sent",
                f"a2a_body_not_json:{type(exc).__name__}",
                retryable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise ToolError("transport_sent", "a2a_body_not_object", retryable=False)
        if self._config.result_field not in payload:
            raise ToolError(
                "transport_sent",
                f"a2a_missing_field:{self._config.result_field}",
                retryable=False,
            )
        result = payload[self._config.result_field]
        return ToolOutput(data={"result": result}, artifacts=[])


def refuse_if_disabled(config: A2ARemoteToolConfig) -> None:
    """Typed check used by catalog wiring (raises ``IntegrationDisabled``)."""
    config.mas.requires(A2A_REMOTE_INTEGRATION_NAME)


# Re-export protocol error for adapters that validate peer shapes.
__all__ = [
    "A2A_REMOTE_INTEGRATION_NAME",
    "IntegrationProtocolError",
    "RemoteA2AAgentTool",
    "refuse_if_disabled",
]
