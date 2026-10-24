"""OpenAI-compatible chat-completions provider (AITunnel / OpenRouter-style APIs)."""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Mapping
from urllib.parse import urljoin

from coscientist.llm.config import LlmConfig
from coscientist.llm.errors import (
    LlmConfigError,
    LlmError,
    LlmFinishReasonError,
    LlmProviderError,
    LlmResponseError,
    LlmTransportError,
)
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
        try:
            response = await self._transport.request(
                "POST",
                url,
                headers=headers,
                body=body,
                timeout_s=self._config.timeout_s,
            )
        except LlmError:
            raise
        except Exception as exc:
            raise LlmTransportError(
                type(exc).__name__,
                retryable=self._config.unexpected_transport_retryable,
            ) from exc
        latency_ms = (time.perf_counter() - started) * 1000.0
        return _parse_response(
            response,
            latency_ms=latency_ms,
            accepted_finish_reasons=self._config.accepted_finish_reasons,
        )


def _read_api_key(env_name: str) -> str:
    value = os.environ.get(env_name)
    if value is None or value.strip() == "":
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


def _parse_response(
    response: TransportResponse,
    *,
    latency_ms: float,
    accepted_finish_reasons: tuple[str, ...],
) -> LlmResponse:
    if response.status_code != 200:
        raise LlmProviderError(
            response.status_code,
            _safe_error_detail(response),
            retry_after_s=_retry_after_s(response.headers),
        )
    try:
        text = response.body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LlmResponseError("response body is not UTF-8") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmResponseError("response body is not JSON") from exc
    if not isinstance(data, dict):
        raise LlmResponseError("response JSON must be an object")
    choice = _first_choice(data)
    content = _content_from_choice(choice)
    finish_reason = _finish_reason_from_choice(choice)
    if finish_reason not in accepted_finish_reasons:
        raise LlmFinishReasonError(finish_reason, content)
    return LlmResponse(
        content=content,
        model=_require_str(data, "model"),
        finish_reason=finish_reason,
        usage=_require_usage(data, latency_ms=latency_ms),
    )


def _safe_error_detail(response: TransportResponse) -> str:
    """Status-only detail; body is not echoed (may contain provider secrets)."""
    return f"provider returned status {response.status_code}"


def _header_get(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _retry_after_s(headers: Mapping[str, str]) -> float | None:
    """Parse Retry-After as non-negative seconds only (no HTTP-date inference)."""
    raw = _header_get(headers, "Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0.0:
        return None
    return value


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value == "":
        raise LlmResponseError(f"missing or empty string field {key!r}")
    return value


def _first_choice(data: Mapping[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmResponseError("missing choices[0]")
    first = choices[0]
    if not isinstance(first, dict):
        raise LlmResponseError("choices[0] must be an object")
    return first


def _content_from_choice(choice: Mapping[str, Any]) -> str:
    message = choice.get("message")
    if not isinstance(message, dict):
        raise LlmResponseError("choices[0].message must be an object")
    content = message.get("content")
    if not isinstance(content, str):
        raise LlmResponseError("choices[0].message.content must be a string")
    return content


def _finish_reason_from_choice(choice: Mapping[str, Any]) -> str:
    reason = choice.get("finish_reason")
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
