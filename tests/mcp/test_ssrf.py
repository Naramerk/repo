"""Adversarial SSRF / redirect / streaming-limit tests for MCP file fetch."""

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
    HttpxTransport,
    McpProtocolError,
    McpResponseLimitError,
    McpSsrfError,
    McpUnsupportedUriError,
)
from coscientist.mcp.files import SafeHttpFileFetcher
from coscientist.mcp.ssrf import host_literal_ip, ip_is_blocked, resolve_and_pin
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
        http_read_chunk_bytes=64,
    )
    return SafeHttpFileFetcher(
        http,
        allowed_schemes=frozenset(cfg.fetch_uri_schemes),
        max_redirect_hops=cfg.max_redirect_hops,
        allowed_content_types=frozenset(cfg.allowed_file_content_types),
        allowed_content_encodings=frozenset(cfg.allowed_content_encodings),
        read_chunk_bytes=cfg.http_read_chunk_bytes,
        resolver=resolver,
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
    with pytest.raises(McpUnsupportedUriError):
        await fetcher.fetch(uri, max_bytes=100, timeout_s=1.0)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",  # CGNAT
        "::1",
        "fe80::1",
        "fc00::1",
        "0.0.0.0",
        "::ffff:127.0.0.1",
        "::ffff:100.64.0.1",
        "224.0.0.1",  # multicast
        "ff02::1",  # multicast
        "fec0::1",  # site-local
    ],
)
def test_blocked_ip_literals(ip: str) -> None:
    assert ip_is_blocked(ip)


def test_public_ips_not_blocked() -> None:
    assert not ip_is_blocked("8.8.8.8")
    assert not ip_is_blocked("2001:4860:4860::8888")


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
async def test_mixed_public_private_answer_denied() -> None:
    http = FakeHttp([])
    resolver = FakeResolver({"cdn.example": ("8.8.8.8", "10.0.0.1")})
    fetcher = _fetcher(http, resolver=resolver)
    with pytest.raises(McpSsrfError):
        await fetcher.fetch("https://cdn.example/a", max_bytes=100, timeout_s=1.0)
    assert http.calls == []


@pytest.mark.asyncio
async def test_dns_rebinding_public_then_private_on_redirect() -> None:
    """First hop resolves public; redirect target re-resolves private → deny."""
    http = FakeHttp([_BufStream(302, {"location": "https://cdn.example/b"}, b"")])
    resolver = FakeResolver([("8.8.8.8",), ("10.0.0.1",)])
    fetcher = _fetcher(http, resolver=resolver)
    with pytest.raises(McpSsrfError):
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
    # Actual connection target is connect_ip; URL keeps original hostname for Host/SNI.
    assert http.calls[0]["url"] == "https://cdn.example/a.pdf"
    assert http.calls[0]["connect_ip"] == "8.8.8.8"
    assert http.calls[0]["server_hostname"] == "cdn.example"
    assert http.calls[0]["headers"]["Host"] == "cdn.example"
    assert "8.8.8.8" not in http.calls[0]["url"]


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
    with pytest.raises(McpProtocolError):
        await fetcher.fetch("https://cdn.example/x.js", max_bytes=100, timeout_s=1.0)


@pytest.mark.asyncio
async def test_httpx_pins_ip_keeps_host_header_against_real_server() -> None:
    """Real local HTTP server: TCP peer is connect_ip; Host is server_hostname."""
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

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(1)
    server = http.server.HTTPServer(("127.0.0.1", port), Handler, bind_and_activate=False)
    server.socket = sock
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        transport = HttpxTransport()
        stream = await transport.open(
            "GET",
            f"http://files.test:{port}/doc.pdf",
            headers={"Host": "files.test"},
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
        thread.join(timeout=2.0)
        server.server_close()

    assert seen["host"].startswith("files.test")
    assert seen["peer"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_httpx_https_sni_and_cert_on_original_hostname() -> None:
    """HTTPS: connect to 127.0.0.1, SNI+cert verify use files.test."""
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
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        tls_sock = ctx.wrap_socket(sock, server_side=True)

        server = http.server.HTTPServer(("127.0.0.1", port), Handler, bind_and_activate=False)
        server.socket = tls_sock
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            transport = HttpxTransport(verify=str(cert_path))
            stream = await transport.open(
                "GET",
                f"https://files.test:{port}/doc.pdf",
                headers={"Host": "files.test"},
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
            thread.join(timeout=2.0)
            server.server_close()

    assert seen["host"].startswith("files.test")


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
    """Create a short-lived self-signed cert via openssl (stdlib test helper)."""
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
