"""OpenAI-compatible chat-completions provider (AITunnel / OpenRouter-style APIs)."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping
from urllib.parse import urljoin

from coscientist.llm.config import LlmConfig
from coscientist.llm.errors import LlmConfigError, LlmProviderError, LlmResponseError
from coscientist.llm.transport import Transport, TransportResponse
from coscientist.llm.types import LlmRequest, LlmResponse, Usage

# Chat Completions path relative to an OpenAI-compatible api_base (…/v1).
_CHAT_COMPLETIONS_PATH = "chat/completions"


class OpenAICompatibleProvider:
    """Maps :class:`LlmRequest` to an OpenAI-compatible HTTP API via an injected transport."""

    def __init__(self, config: LlmConfig, transport: Transport) -> None:
        self._config = config
        self._transport = transport

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """Perform one HTTP attempt. Retries are owned by :class:`LlmClient`."""
        started = time.perf_counter()
        api_key = _read_api_key(self._config.api_key_env)
        url = _chat_url(self._config.base_url)
        body = _encode_request(request, max_completion_tokens=self._config.max_completion_tokens)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        response = await self._transport.request(
            "POST",
            url,
            headers=headers,
            body=body,
            timeout_s=self._config.timeout_s,
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        return _parse_response(response, latency_ms=latency_ms)


def _read_api_key(env_name: str) -> str:
    value = os.environ.get(env_name)
    if value is None or value == "":
        raise LlmConfigError(f"environment variable {env_name!r} is missing or empty")
    return value


def _chat_url(base_url: str) -> str:
    base = base_url if base_url.endswith("/") else base_url + "/"
    return urljoin(base, _CHAT_COMPLETIONS_PATH)


def _encode_request(request: LlmRequest, *, max_completion_tokens: int) -> bytes:
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        "max_tokens": max_completion_tokens,
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _parse_response(response: TransportResponse, *, latency_ms: float) -> LlmResponse:
    if response.status_code != 200:
        raise LlmProviderError(response.status_code, _safe_error_detail(response))
    try:
        data = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LlmResponseError("response body is not JSON") from exc
    if not isinstance(data, dict):
        raise LlmResponseError("response JSON must be an object")
    return LlmResponse(
        content=_require_content(data),
        model=_require_str(data, "model"),
        finish_reason=_require_finish_reason(data),
        usage=_require_usage(data, latency_ms=latency_ms),
    )


def _safe_error_detail(response: TransportResponse) -> str:
    """Status-only detail; body is not echoed (may contain provider secrets)."""
    return f"provider returned status {response.status_code}"


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value == "":
        raise LlmResponseError(f"missing or empty string field {key!r}")
    return value


def _require_content(data: Mapping[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmResponseError("missing choices[0]")
    first = choices[0]
    if not isinstance(first, dict):
        raise LlmResponseError("choices[0] must be an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise LlmResponseError("choices[0].message must be an object")
    content = message.get("content")
    if not isinstance(content, str):
        raise LlmResponseError("choices[0].message.content must be a string")
    return content


def _require_finish_reason(data: Mapping[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmResponseError("missing choices[0]")
    first = choices[0]
    if not isinstance(first, dict):
        raise LlmResponseError("choices[0] must be an object")
    reason = first.get("finish_reason")
    if not isinstance(reason, str) or reason == "":
        raise LlmResponseError("choices[0].finish_reason must be a non-empty string")
    return reason


def _require_usage(data: Mapping[str, Any], *, latency_ms: float) -> Usage:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        raise LlmResponseError("missing usage object")
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if type(prompt) is not int or prompt < 0:
        raise LlmResponseError("usage.prompt_tokens must be a non-negative int")
    if type(completion) is not int or completion < 0:
        raise LlmResponseError("usage.completion_tokens must be a non-negative int")
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        latency_ms=latency_ms,
    )
