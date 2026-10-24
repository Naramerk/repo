"""Stdio lifecycle adversarial tests: stderr flood, grandchildren, partial line."""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from typing import Any

import pytest

from coscientist.mcp import (
    McpClient,
    McpProtocolError,
    McpResponseLimitError,
    McpTimeoutError,
    McpTransportError,
    StdioServerConfig,
)
from coscientist.mcp.stdio import StdioTransport
from tests.mcp.conftest import make_config
from tests.mcp.fake_server import FakeStdioServer, default_handler
from tests.mcp.test_client import RecordingFetcher


@pytest.mark.asyncio
async def test_stderr_flood_trips_max_stderr_bytes() -> None:
    script = textwrap.dedent(
        """
        import sys, json, time
        init = sys.stdin.readline()
        req = json.loads(init)
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "id": req["id"],
            "result": {
                "protocolVersion": req["params"]["protocolVersion"],
                "capabilities": {},
                "serverInfo": {"name": "flood", "version": "0"},
            },
        }) + "\\n")
        sys.stdout.flush()
        sys.stdin.readline()  # initialized
        # Write far past the pipe buffer after overflow; drain must keep reading.
        for _ in range(50):
            sys.stderr.write("Z" * 65536)
            sys.stderr.flush()
        time.sleep(30)
        """
    )
    cfg = make_config(
        servers=(
            StdioServerConfig(
                transport="stdio",
                name="flood",
                command=sys.executable,
                args=("-u", "-c", script),
                env_keys=(),
            ),
        ),
        max_stderr_bytes=4096,
        stdio_read_chunk_bytes=512,
        list_tools_timeout_s=2.0,
        terminate_grace_s=0.2,
        kill_grace_s=0.5,
        process_group_poll_interval_s=0.05,
    )
    client = McpClient(cfg, file_fetcher=RecordingFetcher({}))
    session = await client.connect("flood")
    await asyncio.sleep(0.4)  # let drain task observe overflow while still reading
    with pytest.raises(McpResponseLimitError) as ei:
        await session.list_tools()
    assert ei.value.limit == 4096


@pytest.mark.asyncio
async def test_stdio_timeout_kills_process_group_including_grandchild() -> None:
    script = textwrap.dedent(
        """
        import os, sys, time, json, signal
        if os.fork() == 0:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(60)
            os._exit(0)
        init = sys.stdin.readline()
        req = json.loads(init)
        out = {
            "jsonrpc": "2.0",
            "id": req["id"],
            "result": {
                "protocolVersion": req["params"]["protocolVersion"],
                "capabilities": {},
                "serverInfo": {"name": "hang", "version": "0"},
            },
        }
        sys.stdout.write(json.dumps(out) + "\\n")
        sys.stdout.flush()
        sys.stdin.readline()  # initialized
        time.sleep(60)  # hang; grandchild shares process group
        """
    )
    cfg = make_config(
        servers=(
            StdioServerConfig(
                transport="stdio",
                name="hang",
                command=sys.executable,
                args=("-u", "-c", script),
                env_keys=(),
            ),
        ),
        list_tools_timeout_s=0.3,
        terminate_grace_s=0.15,
        kill_grace_s=0.5,
        process_group_poll_interval_s=0.05,
    )
    client = McpClient(cfg, file_fetcher=RecordingFetcher({}))
    session = await client.connect("hang")
    pgid = session._stdio._pgid  # noqa: SLF001
    with pytest.raises(McpTimeoutError):
        await session.list_tools()
    await asyncio.sleep(cfg.process_group_poll_interval_s * 2)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


@pytest.mark.asyncio
async def test_partial_json_line_fail_closes_session() -> None:
    class BadLine:
        def __init__(self) -> None:
            self._n = 0
            self.closed = False

        async def send(self, message: bytes) -> None:
            return None

        async def receive(self, *, max_bytes: int, timeout_s: float) -> bytes:
            self._n += 1
            return b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"'

        async def aclose(self) -> None:
            self.closed = True

        def reset_receive_buffer(self) -> None:
            return None

    transport = BadLine()
    client = McpClient(make_config(), file_fetcher=RecordingFetcher({}))
    with pytest.raises(McpProtocolError):
        await client.connect_memory("fake", transport)  # type: ignore[arg-type]
    assert transport.closed is True


@pytest.mark.asyncio
async def test_real_process_mid_line_eof() -> None:
    """Stdout closes mid-JSON-line → McpTransportError (stdio.py mid-line EOF path)."""
    script = textwrap.dedent(
        """
        import sys, json
        init = sys.stdin.readline()
        req = json.loads(init)
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "id": req["id"],
            "result": {
                "protocolVersion": req["params"]["protocolVersion"],
                "capabilities": {},
                "serverInfo": {"name": "mid", "version": "0"},
            },
        }) + "\\n")
        sys.stdout.flush()
        sys.stdin.readline()  # initialized
        list_req = sys.stdin.readline()
        req2 = json.loads(list_req)
        # Partial JSON-RPC line, no newline, then exit so the pipe delivers EOF.
        sys.stdout.write('{"jsonrpc":"2.0","id":%s,"result":{"tools":[' % req2["id"])
        sys.stdout.flush()
        raise SystemExit(0)
        """
    )
    cfg = make_config(
        servers=(
            StdioServerConfig(
                transport="stdio",
                name="midline",
                command=sys.executable,
                args=("-u", "-c", script),
                env_keys=(),
            ),
        ),
        list_tools_timeout_s=2.0,
        terminate_grace_s=0.2,
        kill_grace_s=0.5,
        process_group_poll_interval_s=0.05,
    )
    client = McpClient(cfg, file_fetcher=RecordingFetcher({}))
    session = await client.connect("midline")
    with pytest.raises(McpTransportError) as ei:
        await session.list_tools()
    assert "before complete message" in ei.value.detail


@pytest.mark.asyncio
async def test_stdio_send_rejects_embedded_newline() -> None:
    script = textwrap.dedent(
        """
        import sys, time
        time.sleep(30)
        """
    )
    transport = await StdioTransport.start(
        command=sys.executable,
        args=("-u", "-c", script),
        env_keys=(),
        terminate_grace_s=0.2,
        kill_grace_s=0.5,
        process_group_poll_interval_s=0.05,
        read_chunk_bytes=1024,
        max_stderr_bytes=1024,
    )
    try:
        with pytest.raises(McpProtocolError):
            await transport.send(b'{"jsonrpc":"2.0"}\n{"x":1}')
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_response_over_limit_on_memory_transport() -> None:
    def handler(method: str, params: dict[str, Any] | None):
        if method == "tools/list":
            huge = "t" * 5000
            return {
                "tools": [
                    {
                        "name": huge,
                        "description": None,
                        "inputSchema": {"type": "object"},
                    }
                ]
            }
        return default_handler(method, params)

    async with FakeStdioServer(handler) as server:
        client = McpClient(
            make_config(max_response_bytes=200),
            file_fetcher=RecordingFetcher({}),
        )
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(McpResponseLimitError):
            await session.list_tools()
