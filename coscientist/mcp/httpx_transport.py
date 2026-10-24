"""Real HttpTransport: connect to a pinned IP with Host + SNI + cert on the original host."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Mapping
from urllib.parse import urlparse

import httpx

from coscientist.mcp.errors import McpTimeoutError, McpTransportError
from coscientist.mcp.ssrf import rewrite_url_host


class HttpxStream:
    def __init__(self, response: httpx.Response, client: httpx.AsyncClient) -> None:
        self._response = response
        self._client = client
        self.status_code = response.status_code
        self.headers: Mapping[str, str] = {k: v for k, v in response.headers.items()}
        self._aiter: AsyncIterator[bytes] = response.aiter_raw()
        self._pending = bytearray()
        self._eof = False

    async def read(self, n: int) -> bytes:
        if n <= 0:
            return b""
        while len(self._pending) < n and not self._eof:
            try:
                piece = await self._aiter.__anext__()
            except StopAsyncIteration:
                self._eof = True
                break
            self._pending.extend(piece)
        out = bytes(self._pending[:n])
        del self._pending[:n]
        return out

    async def aclose(self) -> None:
        try:
            await self._response.aclose()
        finally:
            await self._client.aclose()


class HttpxTransport:
    """httpx-backed transport that honors ``connect_ip`` + ``server_hostname`` for SNI/cert.

    When ``connect_ip`` is set, TCP connects to that address while ``Host`` and TLS
    SNI/certificate verification use ``server_hostname`` (the original URL host).
    """

    def __init__(self, *, verify: bool | str = True) -> None:
        self._verify = verify

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
    ) -> HttpxStream:
        parsed = urlparse(url)
        original_host = parsed.hostname
        if original_host is None:
            raise McpTransportError("URL has no hostname", retryable=False)

        hdrs = {str(k): str(v) for k, v in headers.items()}
        if connect_ip is not None:
            request_url = rewrite_url_host(url, connect_ip)
            sni = server_hostname if server_hostname is not None else original_host
            hdrs.setdefault("Host", sni)
            extensions: dict[str, object] = {"sni_hostname": sni}
        else:
            request_url = url
            extensions = {}

        timeout = httpx.Timeout(timeout_s)
        client = httpx.AsyncClient(
            timeout=timeout,
            verify=self._verify,
            follow_redirects=False,
            trust_env=False,
        )
        try:
            request = client.build_request(
                method,
                request_url,
                headers=hdrs,
                content=body,
                extensions=extensions,
            )
            response = await client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            await client.aclose()
            raise McpTimeoutError("httpx request exceeded timeout_s") from exc
        except httpx.HTTPError as exc:
            await client.aclose()
            raise McpTransportError(f"httpx transport error: {type(exc).__name__}", retryable=False) from exc

        return HttpxStream(response, client)
