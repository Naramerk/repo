"""Typed LLM client errors. No string matching or shape heuristics."""

from __future__ import annotations


class LlmError(Exception):
    """Base class for LLM client failures."""


class LlmConfigError(LlmError):
    """Missing or invalid configuration / secret environment variable."""


class LlmTimeoutError(LlmError):
    """Transport or call exceeded the configured timeout / total deadline."""


class LlmTransportError(LlmError):
    """Transport failed before a usable HTTP response was obtained."""

    def __init__(self, detail: str, *, retryable: bool) -> None:
        self.detail = detail
        self.retryable = retryable
        super().__init__(detail)


class LlmProviderError(LlmError):
    """Provider returned a non-success HTTP status."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        self.retry_after_s = retry_after_s
        super().__init__(f"HTTP {status_code}: {detail}")


class LlmResponseError(LlmError):
    """Provider response body was not a valid OpenAI-compatible completion."""


class LlmFinishReasonError(LlmError):
    """Completion ended with a finish_reason not listed in ``accepted_finish_reasons``."""

    def __init__(self, finish_reason: str, content: str) -> None:
        self.finish_reason = finish_reason
        self.content = content
        super().__init__(f"finish_reason={finish_reason!r}")


class LlmRetryAfterError(LlmError):
    """Retry-After header was present but invalid, negative, or non-finite."""
