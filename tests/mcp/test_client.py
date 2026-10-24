"""Unit tests for coscientist.mcp — fake in-process stdio, no network."""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

import pytest
from pydantic import ValidationError

from coscientist.mcp import (
    CallToolReply,
    ListToolsReply,
    McpClient,
    McpClientConfig,
    McpConfigError,
    McpProtocolError,
    McpResponseLimitError,
    StdioServerConfig,
    StreamableHttpServerConfig,
)
from coscientist.mcp.digest import content_digest, sha256_hex
from coscientist.mcp.transport import HttpStream
from tests.mcp.conftest import make_config
from tests.mcp.fake_server import FakeStdioServer, default_handler


class _BufStream:
    def __init__(self, status_code: int, headers: Mapping[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = dict(headers)
        self._body = body
        self._offset = 0
        self.bytes_read = 0
        self.closed = False

    async def read(self, n: int) -> bytes:
        if self._offset >= len(self._body):
            return b""
        piece = self._body[self._offset : self._offset + n]
        self._offset += len(piece)
        self.bytes_read += len(piece)
        return piece

    async def aclose(self) -> None:
        self.closed = True


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
        server_hostname: str | None = None,
    ) -> HttpStream:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_s": timeout_s,
                "connect_ip": connect_ip,
                "server_hostname": server_hostname,
            }
        )
        if not self._responses:
            raise AssertionError("unexpected HTTP open")
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
        client = McpClient(make_config(), file_fetcher=RecordingFetcher({}))
        session = await client.connect_memory("fake", server.client)
        listed = await session.list_tools()
        assert isinstance(listed, ListToolsReply)
        assert [t.name for t in listed.tools] == ["echo"]
        assert listed.content_digest == content_digest(listed.result)
        called = await session.call_tool("echo", {"text": "hi"})
        assert isinstance(called, CallToolReply)
        assert called.is_error is False
        assert called.content_digest == content_digest(called.result)
        assert called.files == ()
        await session.aclose()


@pytest.mark.asyncio
async def test_list_tools_follows_next_cursor_pagination() -> None:
    pages = {
        None: {
            "tools": [{"name": "a", "description": None, "inputSchema": {"type": "object"}}],
            "nextCursor": "page-2",
        },
        "page-2": {
            "tools": [{"name": "b", "description": None, "inputSchema": {"type": "object"}}],
        },
    }

    def handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if method == "tools/list":
            cursor = None if params is None else params.get("cursor")
            return pages[cursor]
        return default_handler(method, params)

    async with FakeStdioServer(handler) as server:
        client = McpClient(make_config(), file_fetcher=RecordingFetcher({}))
        session = await client.connect_memory("fake", server.client)
        listed = await session.list_tools()
        assert [t.name for t in listed.tools] == ["a", "b"]
        # raw_bytes keeps every page
        assert listed.raw_bytes.count(b'"name":"a"') == 1
        assert listed.raw_bytes.count(b'"name":"b"') == 1
        await session.aclose()


@pytest.mark.asyncio
async def test_list_tools_page_limit_and_repeated_cursor() -> None:
    def handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if method == "tools/list":
            return {
                "tools": [{"name": "a", "description": None, "inputSchema": {"type": "object"}}],
                "nextCursor": "same",
            }
        return default_handler(method, params)

    async with FakeStdioServer(handler) as server:
        client = McpClient(
            make_config(max_list_tools_pages=3),
            file_fetcher=RecordingFetcher({}),
        )
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(McpProtocolError) as ei:
            await session.list_tools()
        assert "repeated nextCursor" in ei.value.detail


@pytest.mark.asyncio
async def test_list_tools_max_pages_without_repeat() -> None:
    n = 0

    def handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        nonlocal n
        if method == "tools/list":
            n += 1
            return {
                "tools": [
                    {"name": f"t{n}", "description": None, "inputSchema": {"type": "object"}}
                ],
                "nextCursor": f"c{n}",
            }
        return default_handler(method, params)

    async with FakeStdioServer(handler) as server:
        client = McpClient(
            make_config(max_list_tools_pages=2),
            file_fetcher=RecordingFetcher({}),
        )
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(McpProtocolError) as ei:
            await session.list_tools()
        assert "max_list_tools_pages" in ei.value.detail


@pytest.mark.asyncio
async def test_supported_protocol_version_list_not_strict_equality() -> None:
    def handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if method == "initialize":
            return {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "serverInfo": {"name": "x", "version": "0"},
            }
        return default_handler(method, params)

    async with FakeStdioServer(handler) as server:
        client = McpClient(
            make_config(
                protocol_version="2025-06-18",
                supported_protocol_versions=("2025-06-18", "2025-03-26"),
            ),
            file_fetcher=RecordingFetcher({}),
        )
        session = await client.connect_memory("fake", server.client)
        await session.aclose()


@pytest.mark.asyncio
async def test_unsupported_negotiated_version_rejected() -> None:
    def handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if method == "initialize":
            return {
                "protocolVersion": "1999-01-01",
                "capabilities": {},
                "serverInfo": {"name": "x", "version": "0"},
            }
        return default_handler(method, params)

    async with FakeStdioServer(handler) as server:
        client = McpClient(make_config(), file_fetcher=RecordingFetcher({}))
        with pytest.raises(McpProtocolError):
            await client.connect_memory("fake", server.client)


@pytest.mark.asyncio
async def test_jsonrpc_error_keeps_message_and_data() -> None:
    def handler(method: str, params: dict[str, Any] | None) -> Any:
        if method == "tools/call":
            return [
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "error": {
                        "code": -32000,
                        "message": "tool blew up",
                        "data": {"retryable": False},
                    },
                }
            ]
        return default_handler(method, params)

    async with FakeStdioServer(handler) as server:
        client = McpClient(make_config(), file_fetcher=RecordingFetcher({}))
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(McpProtocolError) as ei:
            await session.call_tool("echo", {})
        assert ei.value.rpc_code == -32000
        assert ei.value.rpc_message == "tool blew up"
        assert ei.value.rpc_data == {"retryable": False}
        assert "tool blew up" in str(ei.value)


@pytest.mark.asyncio
async def test_read_loop_skips_notifications_answers_server_requests() -> None:
    def handler(method: str, params: dict[str, Any] | None) -> Any:
        if method == "tools/list":
            return [
                {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}},
                {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "sampling/createMessage",
                    "params": {},
                },
                {
                    "tools": [
                        {
                            "name": "only",
                            "description": None,
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            ]
        return default_handler(method, params)

    async with FakeStdioServer(handler) as server:
        client = McpClient(make_config(), file_fetcher=RecordingFetcher({}))
        session = await client.connect_memory("fake", server.client)
        listed = await session.list_tools()
        assert [t.name for t in listed.tools] == ["only"]
        await session.aclose()


@pytest.mark.asyncio
async def test_timeout_sends_cancelled_notification() -> None:
    class HangServer(FakeStdioServer):
        async def _run(self) -> None:  # type: ignore[override]
            while True:
                raw = await self._to_server.get()
                try:
                    message = json.loads(raw.decode("utf-8"))
                    method = message.get("method")
                    if method == "notifications/cancelled":
                        params = message.get("params") or {}
                        self.cancelled_ids.append(params.get("requestId"))
                        continue
                    if method == "initialize":
                        reply = {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "result": {
                                "protocolVersion": message["params"]["protocolVersion"],
                                "capabilities": {},
                                "serverInfo": {"name": "h", "version": "0"},
                            },
                        }
                        await self._to_client.put(json.dumps(reply).encode())
                        continue
                    if method == "notifications/initialized":
                        continue
                    if method == "tools/list":
                        continue
                finally:
                    self._to_server.task_done()

    async with HangServer(default_handler) as server:
        client = McpClient(
            make_config(list_tools_timeout_s=0.15),
            file_fetcher=RecordingFetcher({}),
        )
        session = await client.connect_memory("fake", server.client)
        from coscientist.mcp import McpTimeoutError

        with pytest.raises(McpTimeoutError):
            await session.list_tools()
        assert server.cancelled_ids == [2]


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
        client = McpClient(make_config(), file_fetcher=fetcher)
        session = await client.connect_memory("fake", server.client)
        reply = await session.call_tool("get_paper", {})
        assert fetcher.urls == [file_url]
        assert len(reply.files) == 1
        f = reply.files[0]
        assert f.key == sha256_hex(file_body)
        assert f.raw_bytes == file_body
        await session.aclose()


@pytest.mark.asyncio
async def test_unknown_server_name_is_config_error() -> None:
    client = McpClient(make_config(), file_fetcher=RecordingFetcher({}))
    with pytest.raises(McpConfigError):
        await client.connect("not-in-profile")


@pytest.mark.asyncio
async def test_streamable_http_session_id_protocol_version_and_notification_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COSCI_TEST_MCP_KEY", "secret-token")
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": "remote", "version": "1"},
        },
    }
    list_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"tools": [{"name": "ping", "description": None, "inputSchema": {"type": "object"}}]},
    }
    http = FakeHttp(
        [
            _BufStream(
                200,
                {"content-type": "application/json", "mcp-session-id": "sess-1"},
                json.dumps(init_body).encode(),
            ),
            _BufStream(202, {}, b""),
            _BufStream(
                200,
                {"content-type": "application/json"},
                json.dumps(list_body).encode(),
            ),
        ]
    )
    cfg = make_config(
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
    session = await client.connect("remote")
    tools = await session.list_tools()
    assert [t.name for t in tools.tools] == ["ping"]
    assert http.calls[0]["headers"]["MCP-Protocol-Version"] == "2025-06-18"
    assert "Mcp-Session-Id" not in http.calls[0]["headers"]
    assert http.calls[1]["headers"]["Mcp-Session-Id"] == "sess-1"
    assert http.calls[1]["headers"]["MCP-Protocol-Version"] == "2025-06-18"
    assert http.calls[1]["timeout_s"] == cfg.notification_timeout_s
    assert http.calls[2]["headers"]["Mcp-Session-Id"] == "sess-1"
    await session.aclose()


@pytest.mark.asyncio
async def test_streamable_http_404_reinitializes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_MCP_KEY", "k")
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": "r", "version": "1"},
        },
    }
    init2 = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": "r", "version": "1"},
        },
    }
    listed = {
        "jsonrpc": "2.0",
        "id": 4,
        "result": {"tools": [{"name": "t", "description": None, "inputSchema": {"type": "object"}}]},
    }
    http = FakeHttp(
        [
            _BufStream(
                200,
                {"content-type": "application/json", "mcp-session-id": "old"},
                json.dumps(init).encode(),
            ),
            _BufStream(202, {}, b""),
            _BufStream(404, {}, b""),  # tools/list with old session
            # re-initialize
            _BufStream(
                200,
                {"content-type": "application/json", "mcp-session-id": "new"},
                json.dumps(init2).encode(),
            ),
            _BufStream(202, {}, b""),
            _BufStream(
                200,
                {"content-type": "application/json"},
                json.dumps(listed).encode(),
            ),
        ]
    )
    cfg = make_config(
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
    session = await client.connect("remote")
    tools = await session.list_tools()
    assert [t.name for t in tools.tools] == ["t"]
    assert any(c["headers"].get("Mcp-Session-Id") == "new" for c in http.calls)
    await session.aclose()


@pytest.mark.asyncio
async def test_streamable_http_sse_early_return_answers_server_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COSCI_TEST_MCP_KEY", "k")
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": "r", "version": "1"},
        },
    }
    # Trailing noise after the matching response must not be fully consumed.
    trailing = ("X" * 8000).encode()
    sse = (
        'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
        'data: {"jsonrpc":"2.0","id":77,"method":"sampling/createMessage","params":{}}\n\n'
        'data: {"jsonrpc":"2.0","id":2,"result":{"tools":'
        '[{"name":"sse","description":null,"inputSchema":{"type":"object"}}]}}\n\n'
        'data: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\n\n'
    ).encode() + trailing
    sse_stream = _BufStream(200, {"content-type": "text/event-stream"}, sse)
    http = FakeHttp(
        [
            _BufStream(
                200,
                {"content-type": "application/json", "mcp-session-id": "s"},
                json.dumps(init).encode(),
            ),
            _BufStream(202, {}, b""),
            sse_stream,
            # -32601 answer for server request id 77
            _BufStream(202, {}, b""),
        ]
    )
    cfg = make_config(
        servers=(
            StreamableHttpServerConfig(
                transport="streamable_http",
                name="remote",
                url="https://mcp.example/mcp",
                api_key_env="COSCI_TEST_MCP_KEY",
            ),
        ),
        http_read_chunk_bytes=256,
    )
    client = McpClient(cfg, file_fetcher=RecordingFetcher({}), http_transport=http)
    session = await client.connect("remote")
    tools = await session.list_tools()
    assert [t.name for t in tools.tools] == ["sse"]
    # Answered server request with Method not found
    answer = json.loads(http.calls[3]["body"].decode())
    assert answer["id"] == 77
    assert answer["error"]["code"] == -32601
    # Early return: did not drain the trailing padding
    assert sse_stream.bytes_read < len(sse)
    await session.aclose()


@pytest.mark.asyncio
async def test_streamable_http_sse_duplicate_id_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_MCP_KEY", "k")
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": "r", "version": "1"},
        },
    }
    # Two matching ids in one "chunked" delivery before early-return can fire —
    # deliver both events in the first read by using a large chunk and a stream
    # that only returns data once the parser would see both if we continued.
    # With early return after first match, duplicate is only visible if both are
    # in the same feed() batch. Put both events in one SSE body chunk.
    sse = (
        'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n\n'
        'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n\n'
    )
    http = FakeHttp(
        [
            _BufStream(
                200,
                {"content-type": "application/json", "mcp-session-id": "s"},
                json.dumps(init).encode(),
            ),
            _BufStream(202, {}, b""),
            _BufStream(200, {"content-type": "text/event-stream"}, sse.encode()),
        ]
    )
    cfg = make_config(
        servers=(
            StreamableHttpServerConfig(
                transport="streamable_http",
                name="remote",
                url="https://mcp.example/mcp",
                api_key_env="COSCI_TEST_MCP_KEY",
            ),
        ),
        # One read returns the whole body so both events are in one feed().
        http_read_chunk_bytes=65536,
    )
    client = McpClient(cfg, file_fetcher=RecordingFetcher({}), http_transport=http)
    session = await client.connect("remote")
    with pytest.raises(McpProtocolError) as ei:
        await session.list_tools()
    assert "duplicate" in ei.value.detail.lower()


@pytest.mark.asyncio
async def test_streamable_http_non_ok_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_MCP_KEY", "k")
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": "r", "version": "1"},
        },
    }
    http = FakeHttp(
        [
            _BufStream(
                200,
                {"content-type": "application/json", "mcp-session-id": "s"},
                json.dumps(init).encode(),
            ),
            _BufStream(202, {}, b""),
            _BufStream(500, {}, b"boom"),
        ]
    )
    cfg = make_config(
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
    session = await client.connect("remote")
    from coscientist.mcp import McpTransportError

    with pytest.raises(McpTransportError):
        await session.list_tools()
    assert session._http._closed is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_missing_api_key_env_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COSCI_TEST_MCP_KEY", raising=False)
    http = FakeHttp([])
    cfg = make_config(
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
            protocol_version="2025-06-18",
            supported_protocol_versions=("2025-06-18",),
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
            max_redirect_hops=0,
            stdio_read_chunk_bytes=1,
            max_stderr_bytes=1,
            http_request_ok_statuses=(200,),
            http_notification_ok_statuses=(202,),
            allowed_file_content_types=("application/pdf",),
            allowed_content_encodings=(),
        )


def test_config_rejects_non_http_fetch_scheme() -> None:
    with pytest.raises(ValidationError):
        make_config(fetch_uri_schemes=("http", "ftp"))


def test_config_rejects_duplicate_server_names() -> None:
    with pytest.raises(ValidationError):
        make_config(
            servers=(
                StdioServerConfig(transport="stdio", name="dup", command="x", args=(), env_keys=()),
                StdioServerConfig(transport="stdio", name="dup", command="y", args=(), env_keys=()),
            )
        )


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
async def test_stdio_missing_env_key_names_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COSCI_MCP_MISSING_SECRET", raising=False)
    cfg = make_config(
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
