"""Transport and file-fetch protocols. Unit tests inject fakes (no network)."""

from __future__ import annotations

from typing import Mapping, Protocol


class HttpStream(Protocol):
    """Incremental body reader; implementations enforce max bytes while reading."""

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    async def read(self, n: int) -> bytes:
        """Read up to ``n`` bytes; empty bytes means EOF."""

    async def aclose(self) -> None: ...


class HttpTransport(Protocol):
    """Byte-level HTTP.

    ``connect_ip`` pins the TCP peer. ``server_hostname`` is used for the Host
    header default, TLS SNI, and certificate verification (must be the original
    URL hostname — never the pinned IP).
    """

    async def open(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_s: float,
        connect_ip: str | None,
        server_hostname: str | None = None,
    ) -> HttpStream: ...


class MessageTransport(Protocol):
    """Framed JSON-RPC message transport."""

    async def send(self, message: bytes) -> None: ...

    async def receive(self, *, max_bytes: int, timeout_s: float) -> bytes: ...

    async def aclose(self) -> None: ...

    def reset_receive_buffer(self) -> None: ...

    async def join_outbound(self) -> None:
        """Wait until queued outbound bytes have been taken by the peer."""


class FileFetcher(Protocol):
    async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes: ...
