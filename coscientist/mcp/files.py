"""Download files referenced by typed MCP URIs under streaming size + SSRF limits."""

from __future__ import annotations

import zlib
from typing import Mapping

from coscientist.mcp.digest import sha256_hex
from coscientist.mcp.errors import (
    McpProtocolError,
    McpResponseLimitError,
    McpSsrfError,
    McpTimeoutError,
    McpTransportError,
)
from coscientist.mcp.ssrf import (
    AddressPolicy,
    AsyncDnsResolver,
    HostnameResolver,
    assert_fetch_scheme,
    redirect_url,
    resolve_and_pin,
)
from coscientist.mcp.transport import FileFetcher, HttpStream, HttpTransport
from coscientist.mcp.types import FetchedFile


class SafeHttpFileFetcher:
    """SSRF-safe file fetcher: resolve+pin, manual redirects, streaming byte limit."""

    def __init__(
        self,
        http: HttpTransport,
        *,
        allowed_schemes: frozenset[str],
        max_redirect_hops: int,
        allowed_content_types: frozenset[str],
        allowed_content_encodings: frozenset[str],
        read_chunk_bytes: int,
        resolver: HostnameResolver | None = None,
        address_policy: AddressPolicy | None = None,
    ) -> None:
        self._http = http
        self._allowed_schemes = allowed_schemes
        self._max_redirect_hops = max_redirect_hops
        self._allowed_content_types = allowed_content_types
        self._allowed_content_encodings = allowed_content_encodings
        self._read_chunk_bytes = read_chunk_bytes
        self._resolver: HostnameResolver = resolver if resolver is not None else AsyncDnsResolver()
        self._address_policy = address_policy

    async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
        assert_fetch_scheme(url, allowed_schemes=self._allowed_schemes)
        current = url
        for hop in range(self._max_redirect_hops + 1):
            _uri, connect_ip, server_hostname = await resolve_and_pin(
                current,
                resolver=self._resolver,
                allowed_schemes=self._allowed_schemes,
                address_policy=self._address_policy,
            )
            headers = {"Host": server_hostname, "Accept-Encoding": "identity"}
            try:
                # Pass the original URL (hostname intact). Transport connects to
                # connect_ip while SNI/cert/Host use server_hostname.
                stream = await self._http.open(
                    "GET",
                    current,
                    headers=headers,
                    body=None,
                    timeout_s=timeout_s,
                    connect_ip=connect_ip,
                    server_hostname=server_hostname,
                )
            except TimeoutError as exc:
                raise McpTimeoutError("file fetch exceeded file_fetch_timeout_s") from exc
            try:
                if stream.status_code in (301, 302, 303, 307, 308):
                    location = _header(stream.headers, "location")
                    if location is None:
                        raise McpSsrfError("redirect without Location", uri=current)
                    await stream.aclose()
                    if hop >= self._max_redirect_hops:
                        raise McpSsrfError(
                            f"redirect hop limit {self._max_redirect_hops} exceeded",
                            uri=current,
                        )
                    current = redirect_url(current, location)
                    continue
                if stream.status_code != 200:
                    raise McpTransportError(
                        f"file fetch status {stream.status_code}",
                        retryable=False,
                    )
                _check_content_type(stream.headers, self._allowed_content_types, uri=current)
                encoding = _content_encoding(stream.headers)
                if encoding is not None and encoding not in self._allowed_content_encodings:
                    raise McpProtocolError(f"Content-Encoding {encoding!r} not in allowed_content_encodings")
                raw = await _read_limited(
                    stream,
                    max_bytes=max_bytes,
                    chunk_bytes=self._read_chunk_bytes,
                )
                if encoding in ("gzip", "x-gzip"):
                    return _gunzip_limited(raw, max_bytes=max_bytes)
                if encoding == "deflate":
                    return _inflate_limited(raw, max_bytes=max_bytes)
                if encoding is not None and encoding != "identity":
                    raise McpProtocolError(f"unsupported Content-Encoding {encoding!r}")
                return raw
            finally:
                await stream.aclose()
        raise McpSsrfError("redirect hop limit exceeded", uri=url)


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


async def _read_limited(
    stream: HttpStream,
    *,
    max_bytes: int,
    chunk_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        piece = await stream.read(min(chunk_bytes, max_bytes - total + 1))
        if not piece:
            break
        total += len(piece)
        if total > max_bytes:
            raise McpResponseLimitError(
                "downloaded file exceeds max_file_bytes while streaming",
                limit=max_bytes,
            )
        chunks.append(piece)
    return b"".join(chunks)


def _gunzip_limited(data: bytes, *, max_bytes: int) -> bytes:
    """Decompress gzip, including multi-member streams, under ``max_bytes``."""
    out = bytearray()
    remaining = data
    while remaining:
        dec = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        room = max_bytes - len(out) + 1
        piece = dec.decompress(remaining, max_length=room)
        out.extend(piece)
        if len(out) > max_bytes:
            raise McpResponseLimitError(
                "decompressed file exceeds max_file_bytes",
                limit=max_bytes,
            )
        if dec.unconsumed_tail and len(out) >= max_bytes:
            raise McpResponseLimitError(
                "decompressed file exceeds max_file_bytes",
                limit=max_bytes,
            )
        out.extend(dec.flush())
        if len(out) > max_bytes:
            raise McpResponseLimitError(
                "decompressed file exceeds max_file_bytes",
                limit=max_bytes,
            )
        remaining = dec.unused_data
        if not remaining:
            break
        if not dec.eof:
            raise McpProtocolError("truncated gzip member")
    return bytes(out)


def _inflate_limited(data: bytes, *, max_bytes: int) -> bytes:
    try:
        dec = zlib.decompressobj()
        out = dec.decompress(data, max_length=max_bytes + 1)
    except zlib.error:
        dec = zlib.decompressobj(wbits=-zlib.MAX_WBITS)
        out = dec.decompress(data, max_length=max_bytes + 1)
    if len(out) > max_bytes:
        raise McpResponseLimitError(
            "decompressed file exceeds max_file_bytes",
            limit=max_bytes,
        )
    out += dec.flush()
    if len(out) > max_bytes:
        raise McpResponseLimitError(
            "decompressed file exceeds max_file_bytes",
            limit=max_bytes,
        )
    return out


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return None


def _content_encoding(headers: Mapping[str, str]) -> str | None:
    raw = _header(headers, "content-encoding")
    if raw is None or raw.strip() == "":
        return None
    # First coding only; multi-coding chains are rejected unless a single allowed token.
    parts = [p.strip().lower() for p in raw.split(",")]
    if len(parts) != 1:
        raise McpProtocolError("Content-Encoding must be a single coding")
    return parts[0]


def _check_content_type(
    headers: Mapping[str, str],
    allowed: frozenset[str],
    *,
    uri: str,
) -> None:
    raw = _header(headers, "content-type")
    if raw is None:
        raise McpProtocolError(f"file response missing Content-Type for {uri!r}")
    media = raw.split(";", 1)[0].strip().lower()
    if media not in allowed:
        raise McpProtocolError(f"Content-Type {media!r} not in allowed_file_content_types")
