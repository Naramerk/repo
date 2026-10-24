"""LlmClient: timeout/retry/limits from config; usage on every successful call."""

from __future__ import annotations

import asyncio
import random
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

    def __init__(
        self,
        config: LlmConfig,
        transport: Transport,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self._config = config
        self._provider: ProviderAdapter = OpenAICompatibleProvider(config, transport)
        self._rng = rng if rng is not None else random.Random()

    async def complete(self, request: LlmRequest) -> LlmResponse:
        started = time.perf_counter()
        deadline = started + self._config.total_deadline_s
        attempts = self._config.max_retries + 1
        last_error: BaseException | None = None
        for index in range(attempts):
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                raise LlmTimeoutError("LLM call exceeded configured total_deadline_s")
            attempt_timeout = (
                self._config.timeout_s
                if self._config.timeout_s <= remaining
                else remaining
            )
            try:
                response = await asyncio.wait_for(
                    self._provider.complete(request),
                    timeout=attempt_timeout,
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
            if index + 1 >= attempts:
                break
            await self._backoff_before_retry(
                attempt_index=index,
                error=last_error,
                deadline=deadline,
            )
        assert last_error is not None
        raise last_error

    async def _backoff_before_retry(
        self,
        *,
        attempt_index: int,
        error: BaseException | None,
        deadline: float,
    ) -> None:
        wait_s = self._compute_wait_s(attempt_index=attempt_index, error=error)
        remaining = deadline - time.perf_counter()
        if remaining <= 0.0:
            raise LlmTimeoutError("LLM call exceeded configured total_deadline_s")
        if wait_s > remaining:
            raise LlmTimeoutError("LLM call exceeded configured total_deadline_s")
        if wait_s > 0.0:
            await asyncio.sleep(wait_s)

    def _compute_wait_s(self, *, attempt_index: int, error: BaseException | None) -> float:
        cfg = self._config
        if (
            cfg.honor_retry_after
            and isinstance(error, LlmProviderError)
            and error.retry_after_s is not None
        ):
            wait_s = error.retry_after_s
        else:
            wait_s = cfg.retry_backoff_s * (cfg.retry_backoff_multiplier**attempt_index)
            if cfg.retry_backoff_jitter_s > 0.0:
                wait_s = wait_s + self._rng.uniform(0.0, cfg.retry_backoff_jitter_s)
        if wait_s > cfg.retry_backoff_max_s:
            return cfg.retry_backoff_max_s
        return wait_s
