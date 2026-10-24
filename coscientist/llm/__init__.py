"""Thin LLM client for the agent layer (M2). No ADK; no coscientist.core imports.

Secrets are read only from environment variables named in config. This package
does not log or print request/response bodies or credentials.
"""

from __future__ import annotations

from coscientist.llm.client import LlmClient
from coscientist.llm.config import LlmConfig
from coscientist.llm.errors import (
    LlmConfigError,
    LlmError,
    LlmFinishReasonError,
    LlmProviderError,
    LlmResponseError,
    LlmRetryAfterError,
    LlmTimeoutError,
    LlmTransportError,
)
from coscientist.llm.openai_compat import OpenAICompatibleProvider
from coscientist.llm.provider import ProviderAdapter
from coscientist.llm.runtime import AsyncSleep, AsyncWaitFor, Clock
from coscientist.llm.transport import Transport, TransportResponse
from coscientist.llm.types import ChatMessage, LlmRequest, LlmResponse, Usage

__all__ = [
    "AsyncSleep",
    "AsyncWaitFor",
    "ChatMessage",
    "Clock",
    "LlmClient",
    "LlmConfig",
    "LlmConfigError",
    "LlmError",
    "LlmFinishReasonError",
    "LlmProviderError",
    "LlmRequest",
    "LlmResponse",
    "LlmResponseError",
    "LlmRetryAfterError",
    "LlmTimeoutError",
    "LlmTransportError",
    "OpenAICompatibleProvider",
    "ProviderAdapter",
    "Transport",
    "TransportResponse",
    "Usage",
]
