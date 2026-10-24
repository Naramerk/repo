"""Coverage for final crocodile review blockers on the MCP client."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from coscientist.mcp import (
    McpError,
    McpProtocolError,
    McpResponseLimitError,
    McpSsrfError,
    McpTimeoutError,
)
from coscientist.mcp.digest import content_digest
from coscientist.mcp.jsonutil import dumps_strict, json_rpc_ids_equal, loads_strict
from coscientist.mcp.ssrf import AsyncDnsResolver, parse_host_port, rewrite_url_host
from coscientist.mcp.urls import uris_from_call_result
from tests.mcp.conftest import (
    FakeClock,
    fake_sleep,
    fake_wait_for,
    make_client,
    make_config,
    system_sleep,
    system_wait_for,
)
from tests.mcp.fake_server import FakeStdioServer, default_handler
from tests.mcp.test_client import FakeHttp, RecordingFetcher
from tests.mcp.test_ssrf import FakeResolver, _fetcher

# --- 1) Strict JSON / JCS -------------------------------------------------


def test_loads_strict_rejects_nan_infinity_and_huge_int() -> None:
    with pytest.raises(McpProtocolError, match="not JSON"):
        loads_strict(b'{"x": NaN}')
    with pytest.raises(McpProtocolError, match="not JSON"):
        loads_strict(b'{"x": Infinity}')
    with pytest.raises(McpProtocolError, match="not JSON"):
        loads_strict(b'{"x": -Infinity}')
    huge = b'{"x": ' + b"1" * 5000 + b"}"
    with pytest.raises(McpProtocolError, match="not JSON"):
        loads_strict(huge)


def test_dumps_strict_rejects_nan() -> None:
    with pytest.raises(McpProtocolError, match="serialization failed"):
        dumps_strict({"x": float("nan")})


def test_content_digest_wraps_rfc8785_failure() -> None:
    with pytest.raises(McpError, match="JCS canonicalization failed"):
        content_digest({"bad": {1, 2, 3}})  # type: ignore[dict-item]


@pytest.mark.asyncio
async def test_session_rejects_nan_in_jsonrpc_response() -> None:
    class NanServer(FakeStdioServer):
        async def _run(self) -> None:  # type: ignore[override]
            while True:
                raw = await self._to_server.get()
                try:
                    message = json.loads(raw.decode("utf-8"))
                    method = message.get("method")
                    if method == "initialize":
                        reply = {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "result": {
                                "protocolVersion": message["params"]["protocolVersion"],
                                "capabilities": {},
                                "serverInfo": {"name": "n", "version": "0"},
                            },
                        }
                        await self._to_client.put(json.dumps(reply).encode())
                        continue
                    if method == "notifications/initialized":
                        continue
                    if method == "tools/list":
                        # Non-strict JSON with NaN — must be rejected by loads_strict.
                        bad = (
                            b'{"jsonrpc":"2.0","id":'
                            + str(message["id"]).encode()
                            + b',"result":{"tools":[],"x":NaN}}'
                        )
                        await self._to_client.put(bad)
                        continue
                finally:
                    self._to_server.task_done()

    async with NanServer(default_handler) as server:
        client = make_client(make_config(), file_fetcher=RecordingFetcher({}))
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(McpProtocolError, match="not JSON"):
            await session.list_tools()


# --- 2) URL / DNS typed errors --------------------------------------------


def test_parse_host_port_invalid_port_is_ssrf_error() -> None:
    with pytest.raises(McpSsrfError, match="host/port parse failed"):
        parse_host_port("http://h:99999/")


def test_parse_host_port_broken_ipv6_is_ssrf_error() -> None:
    with pytest.raises(McpSsrfError, match="host/port parse failed|no hostname"):
        parse_host_port("http://[::1/")


def test_rewrite_url_host_invalid_port_is_ssrf_error() -> None:
    with pytest.raises(McpSsrfError, match="URI parse failed"):
        rewrite_url_host("http://h:99999/x", "8.8.8.8")


@pytest.mark.asyncio
async def test_dns_long_label_is_ssrf_error() -> None:
    host = "a" * 64 + ".example"
    resolver = AsyncDnsResolver()
    with pytest.raises(McpSsrfError, match="DNS resolution"):
        await resolver.resolve(host)


@pytest.mark.asyncio
async def test_fetcher_invalid_port_is_ssrf_error() -> None:
    http = FakeHttp([])
    fetcher = _fetcher(http, resolver=FakeResolver({"h": ("8.8.8.8",)}))
    with pytest.raises(McpSsrfError, match="host/port|URI"):
        await fetcher.fetch("http://h:99999/x", max_bytes=100, timeout_s=1.0)


# --- 3) Typed JSON-RPC id equality ----------------------------------------


def test_json_rpc_ids_equal_is_type_sensitive() -> None:
    assert json_rpc_ids_equal(1, 1) is True
    assert json_rpc_ids_equal("1", "1") is True
    assert json_rpc_ids_equal(1, 1.0) is False
    assert json_rpc_ids_equal(True, 1) is False
    assert json_rpc_ids_equal(None, None) is True


@pytest.mark.asyncio
async def test_session_rejects_float_id_for_int_request() -> None:
    def handler(method: str, params: dict[str, Any] | None) -> Any:
        if method == "tools/list":
            # Fake server normally echoes int ids; force a float id via raw path.
            return {"tools": []}
        return default_handler(method, params)

    class FloatIdServer(FakeStdioServer):
        async def _run(self) -> None:  # type: ignore[override]
            while True:
                raw = await self._to_server.get()
                try:
                    message = json.loads(raw.decode("utf-8"))
                    method = message.get("method")
                    if method == "initialize":
                        reply = {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "result": {
                                "protocolVersion": message["params"]["protocolVersion"],
                                "capabilities": {},
                                "serverInfo": {"name": "f", "version": "0"},
                            },
                        }
                        await self._to_client.put(json.dumps(reply).encode())
                        continue
                    if method == "notifications/initialized":
                        continue
                    if method == "tools/list":
                        reply = {
                            "jsonrpc": "2.0",
                            "id": float(message["id"]),
                            "result": {"tools": []},
                        }
                        await self._to_client.put(
                            json.dumps(reply).encode()
                        )
                        continue
                finally:
                    self._to_server.task_done()

    async with FloatIdServer(handler) as server:
        client = make_client(make_config(), file_fetcher=RecordingFetcher({}))
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(McpProtocolError, match="id does not match|id mismatch"):
            await session.list_tools()


# --- 4) connect cancel does not orphan subprocess -------------------------


@pytest.mark.asyncio
async def test_connect_cancelled_during_initialize_closes_stdio() -> None:
    script = (
        "import sys,json,time\n"
        "sys.stdin.readline()\n"
        "time.sleep(60)\n"
    )
    import sys

    from coscientist.mcp import StdioServerConfig

    cfg = make_config(
        servers=(
            StdioServerConfig(
                transport="stdio",
                name="slow-init",
                command=sys.executable,
                args=("-u", "-c", script),
                env_keys=(),
            ),
        ),
        connect_timeout_s=30.0,
        terminate_grace_s=0.2,
        kill_grace_s=0.5,
        process_group_poll_interval_s=0.05,
    )
    clock = FakeClock()
    client = make_client(
        cfg,
        file_fetcher=RecordingFetcher({}),
        clock=clock,
        sleep=system_sleep(),
        wait_for=fake_wait_for(clock, force_timeout=False),
    )

    async def _connect_and_cancel() -> None:
        task = asyncio.create_task(client.connect("slow-init"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await _connect_and_cancel()
    # No reliable handle to pgid after cancel; ensure a second connect can spawn
    # (previous group should be reaped via BaseException aclose path).
    # Force-timeout initialize so we don't hang:
    client2 = make_client(
        make_config(
            servers=cfg.servers,
            connect_timeout_s=0.2,
            terminate_grace_s=0.2,
            kill_grace_s=0.5,
        ),
        file_fetcher=RecordingFetcher({}),
    )
    with pytest.raises(McpTimeoutError, match="exceeded|timeout"):
        await client2.connect("slow-init")


# --- 5) stdio aclose: _closed after reap; retry after cancel --------------


@pytest.mark.asyncio
async def test_stdio_aclose_sets_closed_only_after_reap() -> None:
    import sys

    from coscientist.mcp.stdio import StdioTransport
    from tests.mcp.conftest import SystemClock, system_sleep

    transport = await StdioTransport.start(
        command=sys.executable,
        args=("-u", "-c", "import time; time.sleep(30)"),
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
    assert transport._closed is False  # noqa: SLF001
    await transport.aclose()
    assert transport._closed is True  # noqa: SLF001
    await transport.aclose()  # idempotent


@pytest.mark.asyncio
async def test_stdio_aclose_retry_after_cancel_during_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    import coscientist.mcp.stdio as stdio_mod
    from coscientist.mcp.stdio import StdioTransport
    from tests.mcp.conftest import SystemClock, system_sleep

    started = asyncio.Event()

    async def hanging_reap(*_a: Any, **_k: Any) -> None:
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(stdio_mod, "_reap_process_group", hanging_reap)

    transport = await StdioTransport.start(
        command=sys.executable,
        args=("-u", "-c", "import time; time.sleep(30)"),
        env_keys=(),
        terminate_grace_s=2.0,
        kill_grace_s=2.0,
        process_group_poll_interval_s=0.05,
        read_chunk_bytes=1024,
        max_stderr_bytes=1024,
        clock=SystemClock(),
        sleep=system_sleep(),
        wait_for=system_wait_for(),
    )
    task = asyncio.create_task(transport.aclose())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport._closed is False  # noqa: SLF001

    # Restore real reap for the cleanup retry.
    monkeypatch.undo()
    await transport.aclose()
    assert transport._closed is True  # noqa: SLF001


# --- 6) Injected clock used for deadlines ---------------------------------


@pytest.mark.asyncio
async def test_injected_clock_drives_list_tools_timeout() -> None:
    class HangServer(FakeStdioServer):
        async def _run(self) -> None:  # type: ignore[override]
            while True:
                raw = await self._to_server.get()
                try:
                    message = json.loads(raw.decode("utf-8"))
                    method = message.get("method")
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
                    # tools/list: never reply
                finally:
                    self._to_server.task_done()

    clock = FakeClock()
    allow = {"n": 0}

    async def wait_for(awaitable: Any, *, timeout_s: float) -> Any:
        allow["n"] += 1
        # Only the initialize response receive is allowed through.
        if allow["n"] == 1:
            return await awaitable
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        clock.advance(timeout_s)
        raise TimeoutError()

    async with HangServer(default_handler) as server:
        client = make_client(
            make_config(list_tools_timeout_s=1.5),
            file_fetcher=RecordingFetcher({}),
            clock=clock,
            sleep=fake_sleep(clock),
            wait_for=wait_for,
        )
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(McpTimeoutError, match="timeout"):
            await session.list_tools()
        assert clock.monotonic() >= 1.5


# --- 7) Embedded resource without text/blob -------------------------------


def test_embedded_resource_without_text_or_blob_is_protocol_error() -> None:
    result = {
        "content": [
            {
                "type": "resource",
                "resource": {"uri": "https://cdn.example/a.pdf", "mimeType": "application/pdf"},
            }
        ]
    }
    with pytest.raises(McpProtocolError, match="requires text or blob"):
        uris_from_call_result(result, allowed_schemes=frozenset({"http", "https"}))


def test_embedded_resource_with_text_is_not_fetched() -> None:
    result = {
        "content": [
            {
                "type": "resource",
                "resource": {
                    "uri": "https://cdn.example/a.pdf",
                    "mimeType": "application/pdf",
                    "text": "hello",
                },
            }
        ]
    }
    assert uris_from_call_result(result, allowed_schemes=frozenset({"http", "https"})) == ()


# --- 8) fetch_files limits ------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_files_enforces_max_files_and_total_bytes() -> None:
    from coscientist.mcp.files import fetch_files
    from tests.mcp.conftest import SystemClock

    class CountingFetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
            self.calls.append(url)
            return b"x" * 10

    fetcher = CountingFetcher()
    with pytest.raises(McpResponseLimitError, match="max_files_per_call") as ei:
        await fetch_files(
            ("https://a/1", "https://a/2", "https://a/3"),
            fetcher=fetcher,  # type: ignore[arg-type]
            max_bytes=100,
            max_files=2,
            max_total_bytes=1000,
            timeout_s=5.0,
            clock=SystemClock(),
        )
    assert ei.value.limit == 2
    assert fetcher.calls == []

    fetcher2 = CountingFetcher()
    with pytest.raises(McpResponseLimitError, match="max_files_total_bytes") as ei2:
        await fetch_files(
            ("https://a/1", "https://a/2"),
            fetcher=fetcher2,  # type: ignore[arg-type]
            max_bytes=100,
            max_files=10,
            max_total_bytes=15,
            timeout_s=5.0,
            clock=SystemClock(),
        )
    assert ei2.value.limit == 15
    assert len(fetcher2.calls) == 2


@pytest.mark.asyncio
async def test_fetch_files_overall_deadline_via_injected_clock() -> None:
    from coscientist.mcp.files import fetch_files

    class SlowFetcher:
        async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
            return b"ok"

    clock = FakeClock(mono=0.0)

    async def wait_for(awaitable: Any, *, timeout_s: float) -> Any:
        clock.advance(timeout_s + 0.1)
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError()

    with pytest.raises(McpTimeoutError, match="files_fetch_timeout_s"):
        await fetch_files(
            ("https://a/1",),
            fetcher=SlowFetcher(),  # type: ignore[arg-type]
            max_bytes=100,
            max_files=10,
            max_total_bytes=1000,
            timeout_s=1.0,
            clock=clock,
            wait_for=wait_for,
        )


@pytest.mark.asyncio
async def test_config_requires_files_limits_without_defaults() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_config().__class__(
            **{
                k: v
                for k, v in make_config().model_dump().items()
                if k
                not in (
                    "max_files_per_call",
                    "max_files_total_bytes",
                    "files_fetch_timeout_s",
                )
            }
        )


# --- 9) import-linter contracts (smoke via lint-imports in CI) ------------


def test_httpx_only_imported_from_httpx_transport() -> None:
    import ast
    from pathlib import Path

    mcp_root = Path("coscientist/mcp")
    offenders: list[str] = []
    for path in mcp_root.rglob("*.py"):
        if path.name == "httpx_transport.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "httpx" or alias.name.startswith("httpx."):
                        offenders.append(str(path))
            if isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "httpx" or node.module.startswith("httpx.")
            ):
                offenders.append(str(path))
    assert offenders == []


# --- 10) timing without wall sleeps covered above / existing tests --------


# --- 11) precise raises already tightened; version negotiation ------------


@pytest.mark.asyncio
async def test_unsupported_protocol_version_match() -> None:
    def handler(method: str, params: dict[str, Any] | None) -> Any:
        if method == "initialize":
            assert params is not None
            return {
                "protocolVersion": "1999-01-01",
                "capabilities": {},
                "serverInfo": {"name": "x", "version": "0"},
            }
        return default_handler(method, params)

    async with FakeStdioServer(handler) as server:
        client = make_client(make_config(), file_fetcher=RecordingFetcher({}))
        with pytest.raises(McpProtocolError, match="not in supported_protocol_versions"):
            await client.connect_memory("fake", server.client)
