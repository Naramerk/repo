"""Download files referenced by typed MCP URIs under an explicit size limit."""

from __future__ import annotations

from coscientist.mcp.digest import sha256_hex
from coscientist.mcp.errors import McpResponseLimitError, McpTimeoutError, McpTransportError
from coscientist.mcp.transport import FileFetcher, HttpTransport
from coscientist.mcp.types import FetchedFile


class HttpFileFetcher:
    """FileFetcher over an injected HttpTransport (GET, no redirects guessed)."""

    def __init__(self, http: HttpTransport) -> None:
        self._http = http

    async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
        try:
            response = await self._http.request(
                "GET",
                url,
                headers={},
                body=None,
                timeout_s=timeout_s,
            )
        except TimeoutError as exc:
            raise McpTimeoutError("file fetch exceeded file_fetch_timeout_s") from exc
        if response.status_code != 200:
            raise McpTransportError(
                f"file fetch status {response.status_code}",
                retryable=False,
            )
        if len(response.body) > max_bytes:
            raise McpResponseLimitError(
                "downloaded file exceeds max_file_bytes",
                limit=max_bytes,
            )
        return response.body


async def fetch_files(
    uris: tuple[str, ...],
    *,
    fetcher: FileFetcher,
    max_bytes: int,
    timeout_s: float,
) -> tuple[FetchedFile, ...]:
    files: list[FetchedFile] = []
    for uri in uris:
        raw = await fetcher.fetch(uri, max_bytes=max_bytes, timeout_s=timeout_s)
        digest = sha256_hex(raw)
        files.append(
            FetchedFile(
                key=digest,
                digest=digest,
                raw_bytes=raw,
                source_uri=uri,
            )
        )
    return tuple(files)
