"""LlmClient: timeout/retry/limits from config; usage on every successful call."""

from __future__ import annotations

import asyncio
import time

from coscientist.llm.config import LlmConfig
from coscientist.llm.errors import (
    LlmProviderError,
    LlmTimeoutError,
    LlmTransportError,
)
from coscientist.llm.openai_compat import OpenAICompatibleProvider
from coscientist.llm.provider import ProviderAdapter
from coscientist.llm.transport import Transport
from coscientist.llm.types import LlmRequest, LlmResponse, Usage


class LlmClient:
    """Thin client over a :class:`ProviderAdapter` with explicit retry policy."""

    def __init__(self, config: LlmConfig, transport: Transport) -> None:
        self._config = config
        self._provider: ProviderAdapter = OpenAICompatibleProvider(config, transport)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        started = time.perf_counter()
        attempts = self._config.max_retries + 1
        last_error: BaseException | None = None
        for index in range(attempts):
            try:
                response = await asyncio.wait_for(
                    self._provider.complete(request),
                    timeout=self._config.timeout_s,
                )
                latency_ms = (time.perf_counter() - started) * 1000.0
                usage = Usage(
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    latency_ms=latency_ms,
                )
                return LlmResponse(
                    content=response.content,
                    model=response.model,
                    finish_reason=response.finish_reason,
                    usage=usage,
                )
            except LlmTimeoutError as exc:
                last_error = exc
                if not self._config.retry_on_timeout or index + 1 >= attempts:
                    raise
            except TimeoutError as exc:
                last_error = LlmTimeoutError("LLM call exceeded configured timeout_s")
                last_error.__cause__ = exc
                if not self._config.retry_on_timeout or index + 1 >= attempts:
                    raise last_error from exc
            except LlmTransportError as exc:
                last_error = exc
                if not exc.retryable or index + 1 >= attempts:
                    raise
            except LlmProviderError as exc:
                last_error = exc
                if exc.status_code not in self._config.retryable_status_codes:
                    raise
                if index + 1 >= attempts:
                    raise
            if index + 1 < attempts and self._config.retry_backoff_s > 0.0:
                await asyncio.sleep(self._config.retry_backoff_s)
        assert last_error is not None
        raise last_error
