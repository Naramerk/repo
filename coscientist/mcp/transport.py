"""Transport and file-fetch protocols. Unit tests inject fakes (no network)."""

from __future__ import annotations

from typing import Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field


class HttpResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int = Field(ge=100, le=599)
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    """Byte-level HTTP used by Streamable HTTP MCP and by file downloads in tests."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_s: float,
    ) -> HttpResponse: ...


class MessageTransport(Protocol):
    """Framed JSON-RPC message transport (one request/response correlation layer above)."""

    async def send(self, message: bytes) -> None: ...

    async def receive(self, *, max_bytes: int, timeout_s: float) -> bytes: ...

    async def aclose(self) -> None: ...


class FileFetcher(Protocol):
    async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes: ...
