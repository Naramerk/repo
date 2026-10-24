"""Stdio lifecycle adversarial tests: stderr flood, grandchildren, partial line."""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from typing import Any

import pytest

from coscientist.mcp import (
    McpProtocolError,
    McpResponseLimitError,
    McpTimeoutError,
    McpTransportError,
    StdioServerConfig,
)
from coscientist.mcp.stdio import StdioTransport
from tests.mcp.conftest import (
    FakeClock,
    SystemClock,
    make_client,
    make_config,
    system_sleep,
    system_wait_for,
)
from tests.mcp.fake_server import FakeStdioServer, default_handler
from tests.mcp.test_client import RecordingFetcher


@pytest.mark.asyncio
async def test_stderr_flood_trips_max_stderr_bytes(tmp_path: Any) -> None:
    from pathlib import Path

    ready = Path(tmp_path) / "flooded"
    script = textwrap.dedent(
        f"""
        import sys, json, time
        init = sys.stdin.readline()
        req = json.loads(init)
        sys.stdout.write(json.dumps({{
            "jsonrpc": "2.0",
            "id": req["id"],
            "result": {{
                "protocolVersion": req["params"]["protocolVersion"],
                "capabilities": {{}},
                "serverInfo": {{"name": "flood", "version": "0"}},
            }},
        }}) + "\\n")
        sys.stdout.flush()
        sys.stdin.readline()  # initialized
        for _ in range(50):
            sys.stderr.write("Z" * 65536)
            sys.stderr.flush()
        open({str(ready)!r}, "w").write("1")
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
    client = make_client(cfg, file_fetcher=RecordingFetcher({}))
    session = await client.connect("flood")

    def _wait_ready() -> None:
        import time

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if ready.exists():
                return
            time.sleep(0.01)
        raise TimeoutError("child never finished stderr flood")

    await asyncio.to_thread(_wait_ready)
    assert session._stdio is not None and session._stdio._stderr_overflow  # noqa: SLF001
    with pytest.raises(McpResponseLimitError, match="max_stderr_bytes") as ei:
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
        list_tools_timeout_s=30.0,
        terminate_grace_s=0.15,
        kill_grace_s=0.5,
        process_group_poll_interval_s=0.05,
    )
    clock = FakeClock()
    allow = {"n": 0}

    async def wait_for(awaitable: Any, *, timeout_s: float) -> Any:
        allow["n"] += 1
        # Allow initialize receive; force-timeout tools/list (no 0.3s wall wait).
        if allow["n"] == 1:
            return await awaitable
        if allow["n"] == 2:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise TimeoutError()
        # Reap / later waits use a real timeout.
        return await asyncio.wait_for(awaitable, timeout=timeout_s)

    client = make_client(
        cfg,
        file_fetcher=RecordingFetcher({}),
        clock=clock,
        sleep=system_sleep(),
        wait_for=wait_for,
    )
    session = await client.connect("hang")
    assert session._stdio is not None
    pgid = session._stdio._pgid  # noqa: SLF001
    with pytest.raises(McpTimeoutError, match="stdio receive exceeded configured timeout"):
        await session.list_tools()

    def _wait_dead() -> None:
        import time

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)
        raise TimeoutError("process group still alive after timeout reap")

    await asyncio.to_thread(_wait_dead)


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

        async def join_outbound(self) -> None:
            return None

    transport = BadLine()
    client = make_client(make_config(), file_fetcher=RecordingFetcher({}))
    with pytest.raises(McpProtocolError, match="not JSON"):
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
    client = make_client(cfg, file_fetcher=RecordingFetcher({}))
    session = await client.connect("midline")
    with pytest.raises(McpTransportError, match="before complete message") as ei:
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
        clock=SystemClock(),
        sleep=system_sleep(),
        wait_for=system_wait_for(),
    )
    try:
        with pytest.raises(McpProtocolError, match="embedded newlines"):
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
        client = make_client(
            make_config(max_response_bytes=200),
            file_fetcher=RecordingFetcher({}),
        )
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(McpResponseLimitError, match="max_response_bytes") as ei:
            await session.list_tools()
        assert ei.value.limit == 200
