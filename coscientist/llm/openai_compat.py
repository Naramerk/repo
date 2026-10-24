"""OpenAI-compatible chat-completions provider (AITunnel / OpenRouter-style APIs)."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urljoin

from coscientist.llm.config import LlmConfig
from coscientist.llm.errors import (
    LlmClockError,
    LlmConfigError,
    LlmError,
    LlmFinishReasonError,
    LlmProviderError,
    LlmResponseError,
    LlmRetryAfterError,
    LlmTransportError,
)
from coscientist.llm.imf_fixdate import parse_imf_fixdate
from coscientist.llm.runtime import Clock
from coscientist.llm.transport import Transport, TransportResponse
from coscientist.llm.types import LlmRequest, LlmResponse, StructuredLlmRequest, Usage


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
            config=self._config,
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
    if isinstance(request, StructuredLlmRequest):
        payload["response_format"] = request.response_format.to_openai_payload()
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _parse_response(
    response: TransportResponse,
    *,
    latency_ms: float,
    config: LlmConfig,
    clock: Clock,
) -> LlmResponse:
    # Success is HTTP 200 only (DESIGN §7.2); any other status is LlmProviderError.
    if response.status_code != 200:
        retry_after_s = _maybe_retry_after_s(
            response,
            config=config,
            clock=clock,
        )
        raise LlmProviderError(
            response.status_code,
            _safe_error_detail(response),
            retry_after_s=retry_after_s,
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
    if finish_reason not in config.accepted_finish_reasons:
        raise LlmFinishReasonError(finish_reason, content)
    return LlmResponse(
        content=content,
        model=_require_str(data, "model"),
        finish_reason=finish_reason,
        usage=_require_usage(data, latency_ms=latency_ms),
    )


def _maybe_retry_after_s(
    response: TransportResponse,
    *,
    config: LlmConfig,
    clock: Clock,
) -> float | None:
    """Parse Retry-After only when honored and the status is retryable (DESIGN §7)."""
    if not config.honor_retry_after:
        return None
    if response.status_code not in config.retryable_status_codes:
        return None
    return _retry_after_s(response.headers, clock)


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


def _ascii_digit_string(value: str) -> bool:
    """RFC 7231 ``1*DIGIT``: non-empty ASCII decimal digits only."""
    return bool(value) and value.isascii() and value.isdigit()


def _strip_ows(value: str) -> str:
    """Strip RFC 7230 OWS only (SP / HTAB); other whitespace is significant/invalid."""
    start = 0
    end = len(value)
    while start < end and value[start] in (" ", "\t"):
        start += 1
    while end > start and value[end - 1] in (" ", "\t"):
        end -= 1
    return value[start:end]


def _retry_after_s(headers: Mapping[str, str], clock: Clock) -> float | None:
    """Parse Retry-After as delta-seconds (1*DIGIT) or IMF-fixdate. Absent → None.

    RFC 850 / asctime forms are rejected (strict IMF-fixdate only for dates).
    """
    raw = _header_get(headers, "Retry-After")
    if raw is None:
        return None
    stripped = _strip_ows(raw)
    if stripped == "":
        raise LlmRetryAfterError("Retry-After header is empty")
    if _ascii_digit_string(stripped):
        try:
            return float(int(stripped))
        except (OverflowError, ValueError) as exc:
            raise LlmRetryAfterError(
                "Retry-After delta-seconds is out of range"
            ) from exc
    return _retry_after_http_date_s(stripped, clock)


def _require_aware_wall_utc(clock: Clock) -> datetime:
    wall = clock.wall_utc()
    if wall.tzinfo is None or wall.utcoffset() is None:
        raise LlmClockError(
            "clock.wall_utc() must return a timezone-aware datetime with a known utcoffset"
        )
    return wall


def _retry_after_http_date_s(raw: str, clock: Clock) -> float:
    """IMF-fixdate delay vs clock. Past dates → 0 (RFC 9110 Retry-After)."""
    when = parse_imf_fixdate(raw)
    delay = (when - _require_aware_wall_utc(clock)).total_seconds()
    if delay < 0.0:
        # RFC 9110 §10.2.3: a date in the past means a delay of 0.
        return 0.0
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
