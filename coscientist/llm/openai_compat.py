"""OpenAI-compatible chat-completions provider (AITunnel / OpenRouter-style APIs)."""

from __future__ import annotations

import json
import math
import os
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping
from urllib.parse import urljoin

from coscientist.llm.config import LlmConfig
from coscientist.llm.errors import (
    LlmConfigError,
    LlmError,
    LlmFinishReasonError,
    LlmProviderError,
    LlmResponseError,
    LlmRetryAfterError,
    LlmTransportError,
)
from coscientist.llm.runtime import Clock
from coscientist.llm.transport import Transport, TransportResponse
from coscientist.llm.types import LlmRequest, LlmResponse, Usage


class OpenAICompatibleProvider:
    """Maps :class:`LlmRequest` to an OpenAI-compatible HTTP API via an injected transport."""

    def __init__(self, config: LlmConfig, transport: Transport, clock: Clock) -> None:
        self._config = config
        self._transport = transport
        self._clock = clock

    async def complete(self, request: LlmRequest, *, timeout_s: float) -> LlmResponse:
        """Perform one HTTP attempt. Retries are owned by :class:`LlmClient`."""
        started = self._clock.monotonic()
        api_key = _read_api_key(self._config.api_key_env)
        url = _chat_url(self._config.base_url, self._config.chat_completions_path)
        body = _encode_request(
            request,
            max_completion_tokens=self._config.max_completion_tokens,
            max_tokens_field=self._config.max_tokens_field,
        )
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
                timeout_s=timeout_s,
            )
        except LlmError:
            raise
        except Exception as exc:
            raise LlmTransportError(
                type(exc).__name__,
                retryable=self._config.unexpected_transport_retryable,
            ) from exc
        latency_ms = (self._clock.monotonic() - started) * 1000.0
        return _parse_response(
            response,
            latency_ms=latency_ms,
            accepted_finish_reasons=self._config.accepted_finish_reasons,
            clock=self._clock,
        )


def _read_api_key(env_name: str) -> str:
    value = os.environ.get(env_name)
    if value is None or value.strip() == "":
        raise LlmConfigError(f"environment variable {env_name!r} is missing or empty")
    return value


def _chat_url(base_url: str, chat_completions_path: str) -> str:
    base = base_url if base_url.endswith("/") else base_url + "/"
    return urljoin(base, chat_completions_path)


def _encode_request(
    request: LlmRequest,
    *,
    max_completion_tokens: int,
    max_tokens_field: str,
) -> bytes:
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        max_tokens_field: max_completion_tokens,
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _parse_response(
    response: TransportResponse,
    *,
    latency_ms: float,
    accepted_finish_reasons: tuple[str, ...],
    clock: Clock,
) -> LlmResponse:
    # Success is HTTP 200 only (DESIGN §7.2); any other status is LlmProviderError.
    if response.status_code != 200:
        raise LlmProviderError(
            response.status_code,
            _safe_error_detail(response),
            retry_after_s=_retry_after_s(response.headers, clock),
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
    found: str | None = None
    for key, value in headers.items():
        if key.lower() == target:
            if found is not None:
                raise LlmResponseError(f"duplicate header {name!r}")
            found = value
    return found


def _retry_after_s(headers: Mapping[str, str], clock: Clock) -> float | None:
    """Parse Retry-After as delta-seconds or HTTP-date. Absent → None; invalid → error."""
    raw = _header_get(headers, "Retry-After")
    if raw is None:
        return None
    stripped = raw.strip()
    if stripped == "":
        raise LlmRetryAfterError("Retry-After header is empty")
    try:
        value = float(stripped)
    except ValueError:
        return _retry_after_http_date_s(stripped, clock)
    if not math.isfinite(value):
        raise LlmRetryAfterError("Retry-After seconds value is not finite")
    if value < 0.0:
        raise LlmRetryAfterError("Retry-After seconds value is negative")
    return value


def _retry_after_http_date_s(raw: str, clock: Clock) -> float:
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError) as exc:
        raise LlmRetryAfterError("Retry-After HTTP-date is invalid") from exc
    if when is None:
        raise LlmRetryAfterError("Retry-After HTTP-date is invalid")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delay = (when - clock.wall_utc()).total_seconds()
    if not math.isfinite(delay):
        raise LlmRetryAfterError("Retry-After HTTP-date delay is not finite")
    if delay < 0.0:
        raise LlmRetryAfterError("Retry-After HTTP-date is in the past")
    return delay


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
