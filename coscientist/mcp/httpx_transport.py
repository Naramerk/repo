"""Real HttpTransport: connect to a pinned IP with Host + SNI + cert on the original host."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from typing import Mapping
from urllib.parse import urlparse

import httpx

from coscientist.mcp.errors import McpSsrfError, McpTimeoutError, McpTransportError
from coscientist.mcp.ssrf import format_host_header, rewrite_url_host


class HttpxStream:
    """Stream over a response; does not own the shared ``AsyncClient``."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
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
            except httpx.TimeoutException as exc:
                raise McpTimeoutError("httpx stream read exceeded timeout") from exc
            except httpx.HTTPError as exc:
                raise McpTransportError(
                    f"httpx stream read error: {type(exc).__name__}",
                    retryable=False,
                ) from exc
            self._pending.extend(piece)
        out = bytes(self._pending[:n])
        del self._pending[:n]
        return out

    async def aclose(self) -> None:
        await self._response.aclose()


class HttpxTransport:
    """httpx-backed transport that honors ``connect_ip`` + ``server_hostname`` for SNI/cert.

    When ``connect_ip`` is set, TCP connects to that address while ``Host`` and TLS
    SNI/certificate verification use ``server_hostname`` (the original URL host).
    One ``AsyncClient`` is reused for the lifetime of this transport.
    """

    def __init__(self, *, verify: bool | ssl.SSLContext = True) -> None:
        self._verify = verify
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                verify=self._verify,
                follow_redirects=False,
                trust_env=False,
            )
        return self._client

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
        try:
            parsed = urlparse(url)
            original_host = parsed.hostname
            parsed_port = parsed.port
            parsed_scheme = parsed.scheme.lower()
        except (ValueError, UnicodeError) as exc:
            raise McpSsrfError(f"URL parse failed: {exc}", uri=url) from exc
        if original_host is None:
            raise McpTransportError("URL has no hostname", retryable=False)

        hdrs = {str(k): str(v) for k, v in headers.items()}
        if connect_ip is not None:
            request_url = rewrite_url_host(url, connect_ip)
            sni = server_hostname if server_hostname is not None else original_host
            hdrs.setdefault(
                "Host",
                format_host_header(sni, parsed_port, scheme=parsed_scheme),
            )
            extensions: dict[str, object] = {"sni_hostname": sni}
        else:
            request_url = url
            extensions = {}

        client = self._ensure_client()
        timeout = httpx.Timeout(timeout_s)
        try:
            request = client.build_request(
                method,
                request_url,
                headers=hdrs,
                content=body,
                timeout=timeout,
                extensions=extensions,
            )
            response = await client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise McpTimeoutError("httpx request exceeded timeout_s") from exc
        except httpx.InvalidURL as exc:
            raise McpTransportError(
                f"httpx invalid URL: {exc}",
                retryable=False,
            ) from exc
        except httpx.HTTPError as exc:
            raise McpTransportError(
                f"httpx transport error: {type(exc).__name__}",
                retryable=False,
            ) from exc

        return HttpxStream(response)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
