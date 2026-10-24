"""Unit tests for coscientist.mcp — fake in-process stdio, no network."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from typing import Any, Mapping

import pytest
from pydantic import ValidationError

from coscientist.mcp import (
    CallToolReply,
    HttpFileFetcher,
    ListToolsReply,
    McpClient,
    McpClientConfig,
    McpConfigError,
    McpProtocolError,
    McpResponseLimitError,
    McpTimeoutError,
    McpUnsupportedUriError,
    StdioServerConfig,
    StreamableHttpServerConfig,
)
from coscientist.mcp.digest import content_digest, sha256_hex
from coscientist.mcp.transport import HttpResponse
from tests.mcp.fake_server import FakeStdioServer, default_handler


def _config(
    *,
    servers: tuple[Any, ...] | None = None,
    max_response_bytes: int = 65536,
    max_file_bytes: int = 4096,
    call_tool_timeout_s: float = 2.0,
    list_tools_timeout_s: float = 2.0,
    connect_timeout_s: float = 2.0,
    terminate_grace_s: float = 0.5,
    kill_grace_s: float = 0.5,
    file_fetch_timeout_s: float = 1.0,
    fetch_uri_schemes: tuple[str, ...] = ("http", "https"),
) -> McpClientConfig:
    if servers is None:
        servers = (
            StdioServerConfig(
                transport="stdio",
                name="fake",
                command=sys.executable,
                args=("-c", "pass"),
                env_keys=(),
            ),
        )
    return McpClientConfig(
        servers=servers,
        protocol_version="2025-03-26",
        client_name="coscientist-test",
        client_version="0.0.0",
        connect_timeout_s=connect_timeout_s,
        list_tools_timeout_s=list_tools_timeout_s,
        call_tool_timeout_s=call_tool_timeout_s,
        terminate_grace_s=terminate_grace_s,
        kill_grace_s=kill_grace_s,
        max_response_bytes=max_response_bytes,
        max_file_bytes=max_file_bytes,
        file_fetch_timeout_s=file_fetch_timeout_s,
        fetch_uri_schemes=fetch_uri_schemes,
    )


class FakeHttp:
    def __init__(self, responses: list[HttpResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_s: float,
    ) -> HttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_s": timeout_s,
            }
        )
        if not self._responses:
            raise AssertionError("unexpected HTTP request")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RecordingFetcher:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self._bodies = bodies
        self.urls: list[str] = []

    async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
        self.urls.append(url)
        body = self._bodies[url]
        if len(body) > max_bytes:
            raise McpResponseLimitError("over", limit=max_bytes)
        return body


@pytest.mark.asyncio
async def test_list_tools_and_call_tool_return_digest_and_raw_bytes() -> None:
    async with FakeStdioServer(default_handler) as server:
        client = McpClient(_config(), file_fetcher=RecordingFetcher({}))
        session = await client.connect_memory("fake", server.client)
        listed = await session.list_tools()
        assert isinstance(listed, ListToolsReply)
        assert [t.name for t in listed.tools] == ["echo"]
        assert listed.content_digest == content_digest(listed.result)
        assert json.loads(listed.raw_bytes.decode("utf-8"))["result"] == listed.result
        called = await session.call_tool("echo", {"text": "hi"})
        assert isinstance(called, CallToolReply)
        assert called.is_error is False
        assert called.content_digest == content_digest(called.result)
        assert called.files == ()
        await session.aclose()


@pytest.mark.asyncio
async def test_call_tool_downloads_resource_link_under_limit() -> None:
    file_url = "https://files.example/paper.pdf"
    file_body = b"%PDF-fake-bytes"

    def handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if method == "tools/call":
            return {
                "content": [
                    {
                        "type": "resource_link",
                        "uri": file_url,
                        "name": "paper",
                    }
                ],
                "isError": False,
            }
        return default_handler(method, params)

    fetcher = RecordingFetcher({file_url: file_body})
    async with FakeStdioServer(handler) as server:
        client = McpClient(_config(), file_fetcher=fetcher)
        session = await client.connect_memory("fake", server.client)
        reply = await session.call_tool("get_paper", {})
        assert fetcher.urls == [file_url]
        assert len(reply.files) == 1
        f = reply.files[0]
        assert f.key == sha256_hex(file_body)
        assert f.digest == f.key
        assert f.raw_bytes == file_body
        assert f.source_uri == file_url
        await session.aclose()


@pytest.mark.asyncio
async def test_unsupported_uri_scheme_is_typed_error() -> None:
    def handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if method == "tools/call":
            return {
                "content": [{"type": "resource_link", "uri": "file:///etc/passwd", "name": "x"}],
                "isError": False,
            }
        return default_handler(method, params)

    async with FakeStdioServer(handler) as server:
        client = McpClient(_config(), file_fetcher=RecordingFetcher({}))
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(McpUnsupportedUriError):
            await session.call_tool("leak", {})
        await session.aclose()


@pytest.mark.asyncio
async def test_file_over_max_bytes_is_limit_error() -> None:
    url = "https://files.example/big.bin"

    def handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if method == "tools/call":
            return {
                "content": [{"type": "resource_link", "uri": url, "name": "big"}],
                "isError": False,
            }
        return default_handler(method, params)

    fetcher = RecordingFetcher({url: b"x" * 100})
    async with FakeStdioServer(handler) as server:
        client = McpClient(_config(max_file_bytes=16), file_fetcher=fetcher)
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(McpResponseLimitError) as ei:
            await session.call_tool("get", {})
        assert ei.value.limit == 16
        await session.aclose()


@pytest.mark.asyncio
async def test_unknown_server_name_is_config_error() -> None:
    client = McpClient(_config(), file_fetcher=RecordingFetcher({}))
    with pytest.raises(McpConfigError):
        await client.connect("not-in-profile")


@pytest.mark.asyncio
async def test_protocol_version_mismatch_is_protocol_error() -> None:
    def handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if method == "initialize":
            return {
                "protocolVersion": "1999-01-01",
                "capabilities": {},
                "serverInfo": {"name": "x", "version": "0"},
            }
        return default_handler(method, params)

    async with FakeStdioServer(handler) as server:
        client = McpClient(_config(), file_fetcher=RecordingFetcher({}))
        with pytest.raises(McpProtocolError):
            await client.connect_memory("fake", server.client)


@pytest.mark.asyncio
async def test_streamable_http_connect_list_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_MCP_KEY", "secret-token")
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "serverInfo": {"name": "remote", "version": "1"},
        },
    }
    list_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"tools": [{"name": "ping", "description": None, "inputSchema": {"type": "object"}}]},
    }
    # initialize result, initialized notification ack, tools/list result
    http = FakeHttp(
        [
            HttpResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=json.dumps(init_body).encode(),
            ),
            HttpResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=b"{}",
            ),
            HttpResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=json.dumps(list_body).encode(),
            ),
        ]
    )
    cfg = _config(
        servers=(
            StreamableHttpServerConfig(
                transport="streamable_http",
                name="remote",
                url="https://mcp.example/mcp",
                api_key_env="COSCI_TEST_MCP_KEY",
            ),
        )
    )
    client = McpClient(cfg, file_fetcher=HttpFileFetcher(http), http_transport=http)
    session = await client.connect("remote")
    tools = await session.list_tools()
    assert [t.name for t in tools.tools] == ["ping"]
    assert http.calls[0]["headers"]["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in tools.raw_bytes.decode("utf-8", errors="replace")
    await session.aclose()


@pytest.mark.asyncio
async def test_missing_api_key_env_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COSCI_TEST_MCP_KEY", raising=False)
    http = FakeHttp([])
    cfg = _config(
        servers=(
            StreamableHttpServerConfig(
                transport="streamable_http",
                name="remote",
                url="https://mcp.example/mcp",
                api_key_env="COSCI_TEST_MCP_KEY",
            ),
        )
    )
    client = McpClient(cfg, file_fetcher=RecordingFetcher({}), http_transport=http)
    with pytest.raises(McpConfigError):
        await client.connect("remote")


@pytest.mark.asyncio
async def test_stdio_subprocess_timeout_kills_process_group() -> None:
    """Real stdio child in its own session; hang → typed timeout + reap."""
    script = textwrap.dedent(
        """
        import os, sys, time
        # Ignore SIGTERM so grace elapses and SIGKILL is required.
        signal = __import__("signal")
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # Minimal initialize reply then hang (no more stdout).
        init = sys.stdin.readline()
        req = __import__("json").loads(init)
        out = {
            "jsonrpc": "2.0",
            "id": req["id"],
            "result": {
                "protocolVersion": req["params"]["protocolVersion"],
                "capabilities": {},
                "serverInfo": {"name": "hang", "version": "0"},
            },
        }
        sys.stdout.write(__import__("json").dumps(out) + "\\n")
        sys.stdout.flush()
        # Consume initialized notification, then hang.
        sys.stdin.readline()
        time.sleep(60)
        """
    )
    cfg = _config(
        servers=(
            StdioServerConfig(
                transport="stdio",
                name="hang",
                command=sys.executable,
                args=("-u", "-c", script),
                env_keys=(),
            ),
        ),
        list_tools_timeout_s=0.2,
        terminate_grace_s=0.1,
        kill_grace_s=0.5,
    )
    client = McpClient(cfg, file_fetcher=RecordingFetcher({}))
    session = await client.connect("hang")
    child_pid = session._stdio._proc.pid  # noqa: SLF001 — test observes kill/reap
    assert child_pid is not None
    with pytest.raises(McpTimeoutError):
        await session.list_tools()
    # Session aclose runs during receive timeout path; ensure reaped.
    await asyncio.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_config_requires_timeouts_and_limits() -> None:
    with pytest.raises(ValidationError):
        McpClientConfig(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name="a",
                    command="x",
                    args=(),
                    env_keys=(),
                ),
            ),
            protocol_version="2025-03-26",
            client_name="c",
            client_version="0",
            list_tools_timeout_s=1.0,
            call_tool_timeout_s=1.0,
            terminate_grace_s=1.0,
            kill_grace_s=1.0,
            max_response_bytes=1,
            max_file_bytes=1,
            file_fetch_timeout_s=1.0,
            fetch_uri_schemes=("https",),
        )


def test_config_rejects_duplicate_server_names() -> None:
    with pytest.raises(ValidationError):
        _config(
            servers=(
                StdioServerConfig(transport="stdio", name="dup", command="x", args=(), env_keys=()),
                StdioServerConfig(transport="stdio", name="dup", command="y", args=(), env_keys=()),
            )
        )


def test_config_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValidationError):
        _config(connect_timeout_s=0.0)


def test_mcp_package_does_not_import_core() -> None:
    import coscientist.mcp as mcp_pkg
    import coscientist.mcp.client as client_mod
    import coscientist.mcp.session as session_mod

    for mod in (mcp_pkg, client_mod, session_mod):
        for name, value in vars(mod).items():
            if name.startswith("_"):
                continue
            modname = getattr(value, "__module__", "") or ""
            assert not modname.startswith("coscientist.core"), modname


@pytest.mark.asyncio
async def test_missing_is_error_rejected() -> None:
    def handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "x"}]}
        return default_handler(method, params)

    async with FakeStdioServer(handler) as server:
        client = McpClient(_config(), file_fetcher=RecordingFetcher({}))
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(McpProtocolError):
            await session.call_tool("echo", {})
        await session.aclose()


@pytest.mark.asyncio
async def test_stdio_missing_env_key_names_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COSCI_MCP_MISSING_SECRET", raising=False)
    cfg = _config(
        servers=(
            StdioServerConfig(
                transport="stdio",
                name="needs-secret",
                command=sys.executable,
                args=("-c", "pass"),
                env_keys=("COSCI_MCP_MISSING_SECRET",),
            ),
        )
    )
    client = McpClient(cfg, file_fetcher=RecordingFetcher({}))
    with pytest.raises(McpConfigError) as ei:
        await client.connect("needs-secret")
    assert "COSCI_MCP_MISSING_SECRET" in str(ei.value)
    # No value could leak — env was absent.
    assert "secret" not in str(ei.value).lower() or "COSCI_MCP_MISSING_SECRET" in str(ei.value)
