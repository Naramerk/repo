"""HTTP transport protocol used by provider adapters. Tests inject a fake.

Implementations must raise :class:`~coscientist.llm.errors.LlmTransportError` or
:class:`~coscientist.llm.errors.LlmTimeoutError` for failures. Any other exception
is wrapped by the OpenAI-compatible provider into ``LlmTransportError`` using
``LlmConfig.unexpected_transport_retryable``.
"""

from __future__ import annotations

from typing import Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field


class TransportResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int = Field(ge=100, le=599)
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_s: float,
    ) -> TransportResponse: ...
