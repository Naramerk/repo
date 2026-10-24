"""HTTP transport for the CLI LLM client (httpx; llm package forbids httpx)."""

from __future__ import annotations

from collections.abc import Mapping

from coscientist.llm.errors import LlmTransportError
from coscientist.llm.transport import TransportResponse


class HttpxTransport:
    """Thin httpx wrapper. Optional ``mcp`` extra provides httpx.

    Retryable is taken only from known transient httpx types (DESIGN §7.2:
    no guessing retryable from generic ``HTTPError`` / ``InvalidURL``).
    """

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_s: float,
    ) -> TransportResponse:
        try:
            import httpx
        except ImportError as exc:
            raise LlmTransportError(
                "httpx_not_installed",
                retryable=False,
            ) from exc
        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method,
                    url,
                    headers=dict(headers),
                    content=body,
                    timeout=timeout_s,
                )
        except httpx.TimeoutException as exc:
            raise LlmTransportError("timeout", retryable=True) from exc
        except (
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ) as exc:
            raise LlmTransportError(type(exc).__name__, retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LlmTransportError(type(exc).__name__, retryable=False) from exc
        except httpx.InvalidURL as exc:
            raise LlmTransportError("InvalidURL", retryable=False) from exc
        hdrs: dict[str, str] = {k.lower(): v for k, v in response.headers.items()}
        return TransportResponse(
            status_code=int(response.status_code),
            headers=hdrs,
            body=response.content,
        )
