"""HTTP transport protocol used by provider adapters. Tests inject a fake."""

from __future__ import annotations

from typing import Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field


class TransportResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int = Field(ge=100, le=599)
    headers: Mapping[str, str] = Field(default_factory=dict)
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
