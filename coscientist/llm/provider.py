"""Provider adapter interface."""

from __future__ import annotations

from typing import Protocol

from coscientist.llm.types import LlmRequest, LlmResponse


class ProviderAdapter(Protocol):
    async def complete(self, request: LlmRequest, *, timeout_s: float) -> LlmResponse: ...
