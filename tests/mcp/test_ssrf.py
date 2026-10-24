"""Adversarial SSRF / redirect / streaming-limit tests for MCP file fetch."""

from __future__ import annotations

import gzip
from typing import Any, Mapping

import pytest

from coscientist.mcp import (
    McpProtocolError,
    McpResponseLimitError,
    McpSsrfError,
    McpUnsupportedUriError,
)
from coscientist.mcp.files import SafeHttpFileFetcher
from coscientist.mcp.ssrf import host_literal_ip, ip_is_blocked
from coscientist.mcp.transport import HttpStream
from tests.mcp.conftest import make_config


class _BufStream:
    def __init__(self, status_code: int, headers: Mapping[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = dict(headers)
        self._body = body
        self._offset = 0

    async def read(self, n: int) -> bytes:
        if self._offset >= len(self._body):
            return b""
        piece = self._body[self._offset : self._offset + n]
        self._offset += len(piece)
        return piece

    async def aclose(self) -> None:
        return None


class FakeHttp:
    def __init__(self, responses: list[_BufStream | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def open(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_s: float,
        connect_ip: str | None,
    ) -> HttpStream:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "connect_ip": connect_ip,
            }
        )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeResolver:
    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self.mapping = mapping
        self.queries: list[str] = []

    async def resolve(self, hostname: str) -> tuple[str, ...]:
        self.queries.append(hostname)
        return self.mapping[hostname]


def _fetcher(
    http: FakeHttp,
    *,
    resolver: FakeResolver | None = None,
    max_redirect_hops: int = 3,
    encodings: tuple[str, ...] = (),
) -> SafeHttpFileFetcher:
    cfg = make_config(
        max_redirect_hops=max_redirect_hops,
        allowed_content_encodings=encodings,
        stdio_read_chunk_bytes=64,
    )
    return SafeHttpFileFetcher(
        http,
        allowed_schemes=frozenset(cfg.fetch_uri_schemes),
        max_redirect_hops=cfg.max_redirect_hops,
        allowed_content_types=frozenset(cfg.allowed_file_content_types),
        allowed_content_encodings=frozenset(cfg.allowed_content_encodings),
        read_chunk_bytes=cfg.stdio_read_chunk_bytes,
        resolver=resolver,
    )


@pytest.mark.parametrize(
    "uri",
    [
        "file:///etc/passwd",
        "ftp://files.example/x",
        "gopher://x",
    ],
)
@pytest.mark.asyncio
async def test_hard_deny_non_http_schemes(uri: str) -> None:
    fetcher = _fetcher(FakeHttp([]))
    with pytest.raises(McpUnsupportedUriError):
        await fetcher.fetch(uri, max_bytes=100, timeout_s=1.0)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "fe80::1",
        "fc00::1",
        "0.0.0.0",
    ],
)
def test_blocked_ip_literals(ip: str) -> None:
    assert ip_is_blocked(ip)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "2130706433",  # decimal loopback
        "0177.0.0.1",  # octal loopback
        "0x7f.0.0.1",  # hex loopback
    ],
)
@pytest.mark.asyncio
async def test_ssrf_blocks_loopback_decimal_octal_hex(host: str) -> None:
    literal = host_literal_ip(host)
    assert literal is not None
    assert ip_is_blocked(literal)
    fetcher = _fetcher(FakeHttp([]))
    with pytest.raises(McpSsrfError):
        await fetcher.fetch(f"http://{host}/x", max_bytes=100, timeout_s=1.0)


@pytest.mark.asyncio
async def test_resolve_pin_rejects_private_dns() -> None:
    http = FakeHttp([])
    resolver = FakeResolver({"evil.example": ("10.1.2.3",)})
    fetcher = _fetcher(http, resolver=resolver)
    with pytest.raises(McpSsrfError):
        await fetcher.fetch("https://evil.example/secret", max_bytes=100, timeout_s=1.0)
    assert http.calls == []


@pytest.mark.asyncio
async def test_resolve_pin_connects_to_pinned_ip() -> None:
    body = b"%PDF-ok"
    http = FakeHttp([_BufStream(200, {"content-type": "application/pdf"}, body)])
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver)
    got = await fetcher.fetch("https://cdn.example/a.pdf", max_bytes=1000, timeout_s=1.0)
    assert got == body
    assert http.calls[0]["connect_ip"] == "8.8.8.8"
    assert http.calls[0]["headers"]["Host"] == "cdn.example"
    assert "8.8.8.8" in http.calls[0]["url"]


@pytest.mark.asyncio
async def test_redirect_checked_per_hop_and_hop_limit() -> None:
    http = FakeHttp(
        [
            _BufStream(302, {"location": "https://cdn.example/b"}, b""),
            _BufStream(302, {"location": "https://cdn.example/c"}, b""),
        ]
    )
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver, max_redirect_hops=1)
    with pytest.raises(McpSsrfError) as ei:
        await fetcher.fetch("https://cdn.example/a", max_bytes=100, timeout_s=1.0)
    assert "hop limit" in ei.value.detail


@pytest.mark.asyncio
async def test_redirect_to_private_ip_denied() -> None:
    http = FakeHttp([_BufStream(302, {"location": "http://127.0.0.1/meta"}, b"")])
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver)
    with pytest.raises(McpSsrfError):
        await fetcher.fetch("https://cdn.example/a", max_bytes=100, timeout_s=1.0)


@pytest.mark.asyncio
async def test_streaming_size_limit_while_reading() -> None:
    big = b"x" * 200
    http = FakeHttp([_BufStream(200, {"content-type": "application/octet-stream"}, big)])
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver)
    with pytest.raises(McpResponseLimitError) as ei:
        await fetcher.fetch("https://cdn.example/big.bin", max_bytes=50, timeout_s=1.0)
    assert ei.value.limit == 50


@pytest.mark.asyncio
async def test_gzip_decompression_bomb_guard() -> None:
    # Small gzip that expands past max_bytes
    raw = b"A" * 5000
    compressed = gzip.compress(raw)
    http = FakeHttp(
        [
            _BufStream(
                200,
                {
                    "content-type": "application/octet-stream",
                    "content-encoding": "gzip",
                },
                compressed,
            )
        ]
    )
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver, encodings=("gzip",))
    with pytest.raises(McpResponseLimitError):
        await fetcher.fetch("https://cdn.example/bomb.gz", max_bytes=100, timeout_s=1.0)


@pytest.mark.asyncio
async def test_content_type_must_be_allowed() -> None:
    http = FakeHttp([_BufStream(200, {"content-type": "application/javascript"}, b"alert(1)")])
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver)
    with pytest.raises(McpProtocolError):
        await fetcher.fetch("https://cdn.example/x.js", max_bytes=100, timeout_s=1.0)
