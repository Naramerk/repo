"""Adversarial SSRF / redirect / streaming-limit / real-socket pin tests."""

from __future__ import annotations

import gzip
import http.server
import socket
import ssl
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

import pytest

from coscientist.mcp import (
    McpProtocolError,
    McpResponseLimitError,
    McpSsrfError,
    McpTimeoutError,
    McpTransportError,
    McpUnsupportedUriError,
)
from coscientist.mcp.files import SafeHttpFileFetcher
from coscientist.mcp.httpx_transport import HttpxTransport
from coscientist.mcp.ssrf import format_host_header, host_literal_ip, ip_is_blocked, resolve_and_pin
from coscientist.mcp.transport import HttpStream
from tests.mcp.conftest import SystemClock, make_config, system_wait_for


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
    """Records the real open() args including connect_ip / server_hostname."""

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
        server_hostname: str | None = None,
    ) -> HttpStream:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "connect_ip": connect_ip,
                "server_hostname": server_hostname,
            }
        )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeResolver:
    def __init__(self, mapping: dict[str, tuple[str, ...]] | list[tuple[str, ...]]) -> None:
        if isinstance(mapping, list):
            self._queue = list(mapping)
            self.mapping: dict[str, tuple[str, ...]] = {}
        else:
            self._queue = None
            self.mapping = mapping
        self.queries: list[str] = []

    async def resolve(self, hostname: str) -> tuple[str, ...]:
        self.queries.append(hostname)
        if self._queue is not None:
            return self._queue.pop(0)
        return self.mapping[hostname]


class AllowLoopbackPolicy:
    def is_blocked(self, ip: str) -> bool:
        return ip not in {"127.0.0.1", "::1"}


def _fetcher(
    http: FakeHttp | HttpxTransport,
    *,
    resolver: FakeResolver | None = None,
    max_redirect_hops: int = 3,
    encodings: tuple[str, ...] = (),
    address_policy: Any = None,
) -> SafeHttpFileFetcher:
    cfg = make_config(
        max_redirect_hops=max_redirect_hops,
        allowed_content_encodings=encodings,
        http_read_chunk_bytes=64,
    )
    return SafeHttpFileFetcher(
        http,
        allowed_schemes=frozenset(cfg.fetch_uri_schemes),
        max_redirect_hops=cfg.max_redirect_hops,
        allowed_content_types=frozenset(cfg.allowed_file_content_types),
        allowed_content_encodings=frozenset(cfg.allowed_content_encodings),
        read_chunk_bytes=cfg.http_read_chunk_bytes,
        clock=SystemClock(),
        wait_for=system_wait_for(),
        resolver=resolver,
        address_policy=address_policy,
    )


@pytest.mark.parametrize(
    "uri",
    [
        "file:///etc/passwd",
        "ftp://files.example/x",
        "gopher://x",
        "data:text/plain,hi",
    ],
)
@pytest.mark.asyncio
async def test_hard_deny_non_http_schemes(uri: str) -> None:
    fetcher = _fetcher(FakeHttp([]))
    with pytest.raises(McpUnsupportedUriError, match="hard-denied"):
        await fetcher.fetch(uri, max_bytes=100, timeout_s=1.0)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "0.0.0.0",
        "::ffff:127.0.0.1",
        "::ffff:100.64.0.1",
        "224.0.0.1",
        "ff02::1",
        "fec0::1",
        "64:ff9b::7f00:1",  # NAT64 → 127.0.0.1
        "::127.0.0.1",  # IPv4-compatible
        "::7f00:1",
        "::ffff:0:7f00:1",  # SIIT → 127.0.0.1
        "::ffff:0:a00:1",  # SIIT → 10.0.0.1
    ],
)
def test_blocked_ip_literals(ip: str) -> None:
    assert ip_is_blocked(ip)


def test_public_ips_not_blocked() -> None:
    assert not ip_is_blocked("8.8.8.8")
    assert not ip_is_blocked("2001:4860:4860::8888")


def test_format_host_header_port_and_ipv6() -> None:
    assert format_host_header("files.test", 8080, scheme="http") == "files.test:8080"
    assert format_host_header("files.test", 443, scheme="https") == "files.test"
    assert format_host_header("2001:db8::1", 8443, scheme="https") == "[2001:db8::1]:8443"
    assert format_host_header("2001:db8::1", 443, scheme="https") == "[2001:db8::1]"


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "2130706433",
        "0177.0.0.1",
        "0x7f.0.0.1",
    ],
)
@pytest.mark.asyncio
async def test_ssrf_blocks_loopback_decimal_octal_hex(host: str) -> None:
    literal = host_literal_ip(host)
    assert literal is not None
    assert ip_is_blocked(literal)
    fetcher = _fetcher(FakeHttp([]))
    with pytest.raises(McpSsrfError, match="blocked IP literal"):
        await fetcher.fetch(f"http://{host}/x", max_bytes=100, timeout_s=1.0)


@pytest.mark.asyncio
async def test_resolve_pin_rejects_private_dns() -> None:
    http = FakeHttp([])
    resolver = FakeResolver({"evil.example": ("10.1.2.3",)})
    fetcher = _fetcher(http, resolver=resolver)
    with pytest.raises(McpSsrfError, match="resolved address .* is blocked"):
        await fetcher.fetch("https://evil.example/secret", max_bytes=100, timeout_s=1.0)
    assert http.calls == []


@pytest.mark.asyncio
async def test_mixed_public_private_answer_denied() -> None:
    http = FakeHttp([])
    resolver = FakeResolver({"cdn.example": ("8.8.8.8", "10.0.0.1")})
    fetcher = _fetcher(http, resolver=resolver)
    with pytest.raises(McpSsrfError, match="resolved address .* is blocked"):
        await fetcher.fetch("https://cdn.example/a", max_bytes=100, timeout_s=1.0)
    assert http.calls == []


@pytest.mark.asyncio
async def test_dns_rebinding_public_then_private_on_redirect() -> None:
    http = FakeHttp([_BufStream(302, {"location": "https://cdn.example/b"}, b"")])
    resolver = FakeResolver([("8.8.8.8",), ("10.0.0.1",)])
    fetcher = _fetcher(http, resolver=resolver)
    with pytest.raises(McpSsrfError, match="resolved address .* is blocked"):
        await fetcher.fetch("https://cdn.example/a", max_bytes=100, timeout_s=1.0)
    assert len(http.calls) == 1
    assert http.calls[0]["connect_ip"] == "8.8.8.8"


@pytest.mark.asyncio
async def test_resolve_pin_connects_to_pinned_ip_not_url_rewrite() -> None:
    body = b"%PDF-ok"
    http = FakeHttp([_BufStream(200, {"content-type": "application/pdf"}, body)])
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver)
    got = await fetcher.fetch("https://cdn.example/a.pdf", max_bytes=1000, timeout_s=1.0)
    assert got == body
    assert http.calls[0]["url"] == "https://cdn.example/a.pdf"
    assert http.calls[0]["connect_ip"] == "8.8.8.8"
    assert http.calls[0]["server_hostname"] == "cdn.example"
    assert http.calls[0]["headers"]["Host"] == "cdn.example"
    assert "8.8.8.8" not in http.calls[0]["url"]


@pytest.mark.asyncio
async def test_host_header_includes_non_default_port() -> None:
    body = b"%PDF-ok"
    http = FakeHttp([_BufStream(200, {"content-type": "application/pdf"}, body)])
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver)
    await fetcher.fetch("https://cdn.example:8443/a.pdf", max_bytes=1000, timeout_s=1.0)
    assert http.calls[0]["headers"]["Host"] == "cdn.example:8443"


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
    with pytest.raises(McpSsrfError, match="hop limit") as ei:
        await fetcher.fetch("https://cdn.example/a", max_bytes=100, timeout_s=1.0)
    assert "hop limit" in ei.value.detail


@pytest.mark.asyncio
async def test_redirect_to_private_ip_denied() -> None:
    http = FakeHttp([_BufStream(302, {"location": "http://127.0.0.1/meta"}, b"")])
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver)
    with pytest.raises(McpSsrfError, match="blocked IP literal"):
        await fetcher.fetch("https://cdn.example/a", max_bytes=100, timeout_s=1.0)


@pytest.mark.asyncio
async def test_streaming_size_limit_while_reading() -> None:
    big = b"x" * 200
    http = FakeHttp([_BufStream(200, {"content-type": "application/octet-stream"}, big)])
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver)
    with pytest.raises(McpResponseLimitError, match="max_file_bytes") as ei:
        await fetcher.fetch("https://cdn.example/big.bin", max_bytes=50, timeout_s=1.0)
    assert ei.value.limit == 50


@pytest.mark.asyncio
async def test_gzip_decompression_bomb_guard() -> None:
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
    with pytest.raises(McpResponseLimitError, match="max_file_bytes") as ei:
        await fetcher.fetch("https://cdn.example/bomb.gz", max_bytes=100, timeout_s=1.0)
    assert ei.value.limit == 100


def _zlib_zeros_bomb(*, uncompressed_mib: int = 32) -> bytes:
    """zlib of zeros that expands far past max_bytes if flush()'d unbounded."""
    import zlib as zlib_mod

    compressor = zlib_mod.compressobj(9)
    chunks: list[bytes] = []
    block = b"\x00" * (1024 * 1024)
    for _ in range(uncompressed_mib):
        chunks.append(compressor.compress(block))
    chunks.append(compressor.flush())
    return b"".join(chunks)


@pytest.mark.asyncio
async def test_deflate_bomb_never_returns_piece_over_max_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every decompress/flush piece must be ≤ max_bytes (fails on unbounded flush)."""
    import zlib as zlib_mod

    import coscientist.mcp.files as files_mod

    max_bytes = 1024
    compressed = _zlib_zeros_bomb(uncompressed_mib=32)
    max_piece = 0
    real_decompressobj = zlib_mod.decompressobj

    class _SpyDecompress:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def decompress(self, data: bytes, max_length: int = 0) -> bytes:
            nonlocal max_piece
            piece = (
                self._inner.decompress(data, max_length)
                if max_length
                else self._inner.decompress(data)
            )
            max_piece = max(max_piece, len(piece))
            return piece

        def flush(self, length: int = zlib_mod.DEF_BUF_SIZE) -> bytes:
            nonlocal max_piece
            piece = self._inner.flush(length)
            max_piece = max(max_piece, len(piece))
            return piece

        @property
        def eof(self) -> bool:
            return self._inner.eof

        @property
        def unconsumed_tail(self) -> bytes:
            return self._inner.unconsumed_tail

        @property
        def unused_data(self) -> bytes:
            return self._inner.unused_data

    def _spy_decompressobj(*args: Any, **kwargs: Any) -> _SpyDecompress:
        return _SpyDecompress(real_decompressobj(*args, **kwargs))

    monkeypatch.setattr(files_mod.zlib, "decompressobj", _spy_decompressobj)

    http = FakeHttp(
        [
            _BufStream(
                200,
                {
                    "content-type": "application/octet-stream",
                    "content-encoding": "deflate",
                },
                compressed,
            )
        ]
    )
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver, encodings=("deflate",))
    with pytest.raises(McpResponseLimitError, match="max_file_bytes") as ei:
        await fetcher.fetch("https://cdn.example/bomb.z", max_bytes=max_bytes, timeout_s=5.0)
    assert ei.value.limit == max_bytes
    # room = max_bytes - len(out) + 1 → a single piece may be max_bytes + 1 before limit raise.
    assert max_piece <= max_bytes + 1, f"decompress returned {max_piece} bytes (> {max_bytes})"


@pytest.mark.asyncio
async def test_truncated_zlib_deflate_is_protocol_error() -> None:
    import zlib as zlib_mod

    wrapped = zlib_mod.compress(b"hello-pdf" * 20)
    truncated = wrapped[:-6]
    http = FakeHttp(
        [
            _BufStream(
                200,
                {
                    "content-type": "application/octet-stream",
                    "content-encoding": "deflate",
                },
                truncated,
            )
        ]
    )
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver, encodings=("deflate",))
    with pytest.raises(McpProtocolError, match="truncated deflate"):
        await fetcher.fetch("https://cdn.example/trunc.z", max_bytes=1000, timeout_s=1.0)


@pytest.mark.asyncio
async def test_trailing_garbage_after_deflate_is_protocol_error() -> None:
    import zlib as zlib_mod

    wrapped = zlib_mod.compress(b"hello-pdf") + b"GARBAGE"
    http = FakeHttp(
        [
            _BufStream(
                200,
                {
                    "content-type": "application/octet-stream",
                    "content-encoding": "deflate",
                },
                wrapped,
            )
        ]
    )
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver, encodings=("deflate",))
    with pytest.raises(McpProtocolError, match="trailing garbage"):
        await fetcher.fetch("https://cdn.example/junk.z", max_bytes=1000, timeout_s=1.0)


@pytest.mark.asyncio
async def test_truncated_gzip_is_protocol_error() -> None:
    compressed = gzip.compress(b"hello-gzip" * 20)
    truncated = compressed[:-10]
    http = FakeHttp(
        [
            _BufStream(
                200,
                {
                    "content-type": "application/octet-stream",
                    "content-encoding": "gzip",
                },
                truncated,
            )
        ]
    )
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver, encodings=("gzip",))
    with pytest.raises(McpProtocolError, match="truncated gzip"):
        await fetcher.fetch("https://cdn.example/trunc.gz", max_bytes=1000, timeout_s=1.0)


@pytest.mark.asyncio
async def test_trailing_garbage_after_gzip_is_protocol_error() -> None:
    compressed = gzip.compress(b"hello-gzip") + b"GARBAGE"
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
    with pytest.raises(McpProtocolError, match="trailing garbage after gzip"):
        await fetcher.fetch("https://cdn.example/junk.gz", max_bytes=1000, timeout_s=1.0)


@pytest.mark.asyncio
async def test_empty_gzip_and_deflate_bodies_are_protocol_errors() -> None:
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    for encoding, match in (("gzip", "truncated gzip"), ("deflate", "truncated deflate")):
        http = FakeHttp(
            [
                _BufStream(
                    200,
                    {
                        "content-type": "application/octet-stream",
                        "content-encoding": encoding,
                    },
                    b"",
                )
            ]
        )
        fetcher = _fetcher(http, resolver=resolver, encodings=(encoding,))
        with pytest.raises(McpProtocolError, match=match):
            await fetcher.fetch(f"https://cdn.example/empty.{encoding}", max_bytes=100, timeout_s=1.0)


@pytest.mark.asyncio
async def test_multi_member_gzip_decompressed() -> None:
    part1 = gzip.compress(b"hello ")
    part2 = gzip.compress(b"world")
    http = FakeHttp(
        [
            _BufStream(
                200,
                {
                    "content-type": "application/octet-stream",
                    "content-encoding": "gzip",
                },
                part1 + part2,
            )
        ]
    )
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver, encodings=("gzip",))
    got = await fetcher.fetch("https://cdn.example/multi.gz", max_bytes=100, timeout_s=1.0)
    assert got == b"hello world"


@pytest.mark.asyncio
async def test_content_type_must_be_allowed() -> None:
    http = FakeHttp([_BufStream(200, {"content-type": "application/javascript"}, b"alert(1)")])
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver)
    with pytest.raises(McpProtocolError, match="not in allowed_file_content_types"):
        await fetcher.fetch("https://cdn.example/x.js", max_bytes=100, timeout_s=1.0)


def _start_http_server(
    handler_cls: type[http.server.BaseHTTPRequestHandler],
) -> tuple[http.server.HTTPServer, int, threading.Thread]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(8)
    server = http.server.HTTPServer(("127.0.0.1", port), handler_cls, bind_and_activate=False)
    server.socket = sock
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


@pytest.mark.asyncio
async def test_httpx_pins_ip_keeps_host_header_against_real_server() -> None:
    seen: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen["host"] = self.headers.get("Host", "")
            seen["peer"] = self.connection.getpeername()[0]
            body = b"%PDF-pinned"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server, port, thread = _start_http_server(Handler)
    transport = HttpxTransport()
    try:
        stream = await transport.open(
            "GET",
            f"http://files.test:{port}/doc.pdf",
            headers={},
            body=None,
            timeout_s=2.0,
            connect_ip="127.0.0.1",
            server_hostname="files.test",
        )
        try:
            assert stream.status_code == 200
            chunks: list[bytes] = []
            while True:
                piece = await stream.read(64)
                if not piece:
                    break
                chunks.append(piece)
            assert b"".join(chunks) == b"%PDF-pinned"
        finally:
            await stream.aclose()
    finally:
        await transport.aclose()
        server.shutdown()
        thread.join(timeout=2.0)
        server.server_close()

    assert seen["host"] == f"files.test:{port}"
    assert seen["peer"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_httpx_https_sni_and_cert_on_original_hostname() -> None:
    seen: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen["host"] = self.headers.get("Host", "")
            body = b"%PDF-tls"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    with tempfile.TemporaryDirectory() as tmp:
        cert_path, key_path = _make_self_signed(Path(tmp), common_name="files.test")
        ctx_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx_server.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        tls_sock = ctx_server.wrap_socket(sock, server_side=True)
        server = http.server.HTTPServer(("127.0.0.1", port), Handler, bind_and_activate=False)
        server.socket = tls_sock
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        verify = ssl.create_default_context()
        verify.load_verify_locations(str(cert_path))
        # CN-only certs need hostname check against files.test via SNI
        verify.check_hostname = True
        transport = HttpxTransport(verify=verify)
        try:
            stream = await transport.open(
                "GET",
                f"https://files.test:{port}/doc.pdf",
                headers={},
                body=None,
                timeout_s=2.0,
                connect_ip="127.0.0.1",
                server_hostname="files.test",
            )
            try:
                assert stream.status_code == 200
                chunks: list[bytes] = []
                while True:
                    piece = await stream.read(64)
                    if not piece:
                        break
                    chunks.append(piece)
                assert b"".join(chunks) == b"%PDF-tls"
            finally:
                await stream.aclose()
        finally:
            await transport.aclose()
            thread.join(timeout=2.0)
            server.server_close()

    assert seen["host"].startswith("files.test")


@pytest.mark.asyncio
async def test_httpx_sni_cert_mismatch_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cert_path, key_path = _make_self_signed(Path(tmp), common_name="files.test")
        ctx_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx_server.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        tls_sock = ctx_server.wrap_socket(sock, server_side=True)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

        server = http.server.HTTPServer(("127.0.0.1", port), Handler, bind_and_activate=False)
        server.socket = tls_sock
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        verify = ssl.create_default_context()
        verify.load_verify_locations(str(cert_path))
        transport = HttpxTransport(verify=verify)
        try:
            with pytest.raises(McpTransportError, match="httpx transport error"):
                await transport.open(
                    "GET",
                    f"https://evil.example:{port}/",
                    headers={},
                    body=None,
                    timeout_s=2.0,
                    connect_ip="127.0.0.1",
                    server_hostname="evil.example",
                )
        finally:
            await transport.aclose()
            thread.join(timeout=2.0)
            server.server_close()


@pytest.mark.asyncio
async def test_e2e_fetcher_redirect_over_real_socket() -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/a":
                self.send_response(302)
                self.send_header("Location", "/b")
                self.end_headers()
                return
            body = b"%PDF-redir"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server, port, thread = _start_http_server(Handler)
    transport = HttpxTransport()
    resolver = FakeResolver({"files.test": ("127.0.0.1",)})
    fetcher = _fetcher(
        transport,
        resolver=resolver,
        address_policy=AllowLoopbackPolicy(),
    )
    try:
        got = await fetcher.fetch(f"http://files.test:{port}/a", max_bytes=1000, timeout_s=2.0)
        assert got == b"%PDF-redir"
    finally:
        await transport.aclose()
        server.shutdown()
        thread.join(timeout=2.0)
        server.server_close()


@pytest.mark.asyncio
async def test_e2e_fetcher_dns_rebinding_denied_over_real_socket() -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://files.test/next")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server, port, thread = _start_http_server(Handler)
    transport = HttpxTransport()
    # First answer loopback (allowed by test policy); redirect re-resolve → private.
    resolver = FakeResolver([("127.0.0.1",), ("10.0.0.1",)])
    fetcher = _fetcher(
        transport,
        resolver=resolver,
        address_policy=AllowLoopbackPolicy(),
    )
    try:
        with pytest.raises(McpSsrfError, match="resolved address .* is blocked"):
            await fetcher.fetch(f"http://files.test:{port}/a", max_bytes=1000, timeout_s=2.0)
    finally:
        await transport.aclose()
        server.shutdown()
        thread.join(timeout=2.0)
        server.server_close()


@pytest.mark.asyncio
async def test_e2e_fetcher_mixed_answer_denied_over_real_socket() -> None:
    """Hop1 connects on loopback; hop2 mixed public+private answer is denied."""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://files.test/next")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server, port, thread = _start_http_server(Handler)
    transport = HttpxTransport()
    # First resolve: only loopback → real socket. Second: mixed → deny before connect.
    resolver = FakeResolver([("127.0.0.1",), ("127.0.0.1", "10.0.0.1")])
    fetcher = _fetcher(
        transport,
        resolver=resolver,
        address_policy=AllowLoopbackPolicy(),
    )
    try:
        with pytest.raises(McpSsrfError, match="resolved address .* is blocked"):
            await fetcher.fetch(f"http://files.test:{port}/a", max_bytes=1000, timeout_s=2.0)
        assert hits == ["/a"], f"expected real-socket hop1 hit, got {hits!r}"
    finally:
        await transport.aclose()
        server.shutdown()
        thread.join(timeout=2.0)
        server.server_close()


@pytest.mark.asyncio
async def test_e2e_slow_drip_honors_overall_fetch_deadline() -> None:
    """Redirect + 1 byte/0.3s must not outlive timeout_s=1.0 (overall wait_for)."""

    port_box: dict[str, int] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            import time

            if self.path == "/start":
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://files.test:{port_box['port']}/drip",
                )
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "40")
            self.end_headers()
            for _ in range(40):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.3)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server, port, thread = _start_http_server(Handler)
    port_box["port"] = port
    transport = HttpxTransport()
    resolver = FakeResolver([("127.0.0.1",), ("127.0.0.1",)])
    fetcher = _fetcher(
        transport,
        resolver=resolver,
        address_policy=AllowLoopbackPolicy(),
    )
    try:
        with pytest.raises(McpTimeoutError, match="file fetch exceeded"):
            await fetcher.fetch(f"http://files.test:{port}/start", max_bytes=1000, timeout_s=1.0)
    finally:
        await transport.aclose()
        server.shutdown()
        thread.join(timeout=2.0)
        server.server_close()


@pytest.mark.asyncio
async def test_deflate_zlib_ok_raw_deflate_is_protocol_error() -> None:
    import zlib as zlib_mod

    # zlib-wrapped deflate (RFC 1950) — the only accepted deflate form
    wrapped = zlib_mod.compress(b"hello-pdf")
    http = FakeHttp(
        [
            _BufStream(
                200,
                {
                    "content-type": "application/octet-stream",
                    "content-encoding": "deflate",
                },
                wrapped,
            )
        ]
    )
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    fetcher = _fetcher(http, resolver=resolver, encodings=("deflate",))
    got = await fetcher.fetch("https://cdn.example/a", max_bytes=100, timeout_s=1.0)
    assert got == b"hello-pdf"

    # raw deflate (RFC 1951) must NOT silently succeed
    raw = zlib_mod.compress(b"hello-pdf")[2:-4]  # strip zlib header/trailer approx
    # Use wbits=-15 produce raw:
    c = zlib_mod.compressobj(wbits=-zlib_mod.MAX_WBITS)
    raw = c.compress(b"hello-pdf") + c.flush()
    http2 = FakeHttp(
        [
            _BufStream(
                200,
                {
                    "content-type": "application/octet-stream",
                    "content-encoding": "deflate",
                },
                raw,
            )
        ]
    )
    fetcher2 = _fetcher(http2, resolver=resolver, encodings=("deflate",))
    with pytest.raises(McpProtocolError, match="deflate"):
        await fetcher2.fetch("https://cdn.example/b", max_bytes=100, timeout_s=1.0)


@pytest.mark.asyncio
async def test_httpx_timeout_maps_to_mcp_timeout() -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            import time

            time.sleep(2.0)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server, port, thread = _start_http_server(Handler)
    transport = HttpxTransport()
    try:
        with pytest.raises(McpTimeoutError, match="httpx request exceeded timeout_s"):
            await transport.open(
                "GET",
                f"http://files.test:{port}/slow",
                headers={},
                body=None,
                timeout_s=0.1,
                connect_ip="127.0.0.1",
                server_hostname="files.test",
            )
    finally:
        await transport.aclose()
        server.shutdown()
        thread.join(timeout=3.0)
        server.server_close()


@pytest.mark.asyncio
async def test_httpx_connect_error_maps_to_mcp_transport_error() -> None:
    transport = HttpxTransport()
    try:
        with pytest.raises(McpTransportError, match="httpx transport error"):
            await transport.open(
                "GET",
                "http://files.test:1/",
                headers={},
                body=None,
                timeout_s=1.0,
                connect_ip="127.0.0.1",
                server_hostname="files.test",
            )
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_resolve_and_pin_returns_server_hostname() -> None:
    resolver = FakeResolver({"cdn.example": ("8.8.8.8",)})
    uri, connect_ip, server_hostname = await resolve_and_pin(
        "https://cdn.example/x",
        resolver=resolver,
        allowed_schemes=frozenset({"http", "https"}),
    )
    assert uri == "https://cdn.example/x"
    assert connect_ip == "8.8.8.8"
    assert server_hostname == "cdn.example"


def _make_self_signed(directory: Path, *, common_name: str) -> tuple[Path, Path]:
    import subprocess

    cert = directory / "cert.pem"
    key = directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            f"/CN={common_name}",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key
