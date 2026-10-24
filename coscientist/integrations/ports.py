"""Narrow Protocols for capability #21 — agent layer / MCP client wire these later.

Tests inject fakes; this package does not import ``coscientist.core`` or ``mcp``.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class JournalEventView(BaseModel):
    """Public journal event projection for A2A/MCP consumers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: StrictInt
    study_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    entity_key: str
    payload: dict[str, Any]
    schema_version: StrictInt


class StudyCreatePort(Protocol):
    """Create a study (wraps ``POST /studies`` semantics)."""

    async def create_study(
        self,
        *,
        profile: str,
        request: str,
        catalog_version: int,
        idempotency_key: str | None,
    ) -> str: ...


class JournalStreamPort(Protocol):
    """Read journal events after a sequence number for one study."""

    async def read_after(self, study_id: str, seq: int) -> list[JournalEventView]: ...


class ModuleHandler(Protocol):
    """One CoScientist module exposed as an MCP tool (hypotheses / literature / plan)."""

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]: ...


class A2ATransportResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: StrictInt = Field(ge=100, le=599)
    headers: Mapping[str, str]
    body: bytes


class A2AHttpTransport(Protocol):
    """Injected HTTP transport for outbound A2A (no httpx in tools/api)."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_s: float,
    ) -> A2ATransportResponse: ...
