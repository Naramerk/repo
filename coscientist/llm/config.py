"""LLM client configuration. Every operational value is required (no guessed defaults)."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LlmConfig(BaseModel):
    """Explicit timeout, retry, and limits. API key is read from ``api_key_env`` at call time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    timeout_s: float
    total_deadline_s: float
    max_retries: int = Field(ge=0)
    retry_backoff_s: float
    retry_backoff_multiplier: float
    retry_backoff_jitter_s: float
    retry_backoff_max_s: float
    retry_on_timeout: bool
    honor_retry_after: bool
    retryable_status_codes: tuple[int, ...]
    max_completion_tokens: int = Field(ge=1)
    accepted_finish_reasons: tuple[str, ...]
    unexpected_transport_retryable: bool

    @field_validator("base_url")
    @classmethod
    def _base_url_no_whitespace(cls, v: str) -> str:
        if v != v.strip() or any(ch.isspace() for ch in v):
            raise ValueError("base_url must not contain whitespace")
        return v

    @field_validator("api_key_env")
    @classmethod
    def _api_key_env_name(cls, v: str) -> str:
        if v != v.strip() or any(ch.isspace() for ch in v):
            raise ValueError("api_key_env must be an environment variable name without whitespace")
        return v

    @field_validator("timeout_s", "total_deadline_s")
    @classmethod
    def _positive_finite(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0.0:
            raise ValueError("must be finite and > 0")
        return v

    @field_validator("retry_backoff_s", "retry_backoff_jitter_s", "retry_backoff_max_s")
    @classmethod
    def _non_negative_finite(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0.0:
            raise ValueError("must be finite and >= 0")
        return v

    @field_validator("retry_backoff_multiplier")
    @classmethod
    def _multiplier_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0.0:
            raise ValueError("retry_backoff_multiplier must be finite and > 0")
        return v

    @field_validator("retryable_status_codes")
    @classmethod
    def _status_codes(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        for code in v:
            if not (100 <= code <= 599):
                raise ValueError(f"retryable status code out of range: {code}")
        return v

    @field_validator("accepted_finish_reasons")
    @classmethod
    def _accepted_reasons(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("accepted_finish_reasons must be non-empty")
        for reason in v:
            if not isinstance(reason, str) or reason == "" or reason != reason.strip():
                raise ValueError("accepted_finish_reasons entries must be non-empty stripped strings")
        return v
