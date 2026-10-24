"""Coverage for final crocodile review blockers on the MCP client."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.mcp import (
    IntegerDomainError,
    McpError,
    McpProtocolError,
    McpResponseLimitError,
    McpSsrfError,
    McpTimeoutError,
    McpTransportError,
    StdioServerConfig,
)
from coscientist.mcp.digest import content_digest
from coscientist.mcp.files import fetch_files
from coscientist.mcp.httpx_transport import HttpxTransport
from coscientist.mcp.jsonutil import dumps_strict, json_rpc_ids_equal, loads_strict
from coscientist.mcp.ssrf import AsyncDnsResolver, parse_host_port, rewrite_url_host
from coscientist.mcp.stdio import StdioTransport
from coscientist.mcp.urls import uris_from_call_result
from tests.mcp.conftest import (
    FakeClock,
    SystemClock,
    fake_sleep,
    make_client,
    make_config,
    system_sleep,
    system_wait_for,
)
from tests.mcp.fake_server import FakeStdioServer, default_handler
from tests.mcp.test_client import RecordingFetcher
from tests.mcp.test_ssrf import FakeResolver, _fetcher

_INT_MAX = 2**53 - 1
_OUT = 2**53  # exactly ±2^53 — out of RFC 8785 domain


# --- 1) Strict JSON / IntegerDomainError / JCS ----------------------------


def test_loads_strict_rejects_nan_infinity_and_huge_digit_string() -> None:
    with pytest.raises(McpProtocolError, match="not JSON"):
        loads_strict(b'{"x": NaN}')
    with pytest.raises(McpProtocolError, match="not JSON"):
        loads_strict(b'{"x": Infinity}')
    huge = b'{"x": ' + b"1" * (sys.get_int_max_str_digits() + 10) + b"}"
    with pytest.raises(McpProtocolError, match="not JSON"):
        loads_strict(huge)


def test_loads_strict_rejects_exactly_plus_minus_2_pow_53() -> None:
    """±2^53 → IntegerDomainError (not bare McpError from JCS digest)."""
    for value in (_OUT, -_OUT):
        raw = json.dumps({"x": value}).encode()
        with pytest.raises(IntegerDomainError, match=r"outside ±2\^53-1 domain") as ei:
            loads_strict(raw)
        assert ei.value.value == value
        assert type(ei.value) is IntegerDomainError


def test_loads_strict_accepts_int_max_safe() -> None:
    assert loads_strict(json.dumps({"x": _INT_MAX, "y": -_INT_MAX}).encode()) == {
        "x": _INT_MAX,
        "y": -_INT_MAX,
    }


def test_loads_strict_refuses_when_int_max_str_digits_unlimited() -> None:
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        with pytest.raises(McpProtocolError, match="int_max_str_digits is 0"):
            loads_strict(b'{"x": 1}')
    finally:
        sys.set_int_max_str_digits(previous)


def test_dumps_strict_rejects_nan() -> None:
    with pytest.raises(McpProtocolError, match="serialization failed"):
        dumps_strict({"x": float("nan")})


def test_content_digest_wraps_rfc8785_failure() -> None:
    with pytest.raises(McpError, match="JCS canonicalization failed"):
        content_digest({"bad": {1, 2, 3}})  # type: ignore[dict-item]


@pytest.mark.asyncio
async def test_integer_domain_error_before_file_fetch_fail_closes() -> None:
    """Int > 2^53 (under digit cap) → IntegerDomainError, no download, session closed."""
    file_url = "https://cdn.example/a.pdf"

    class BigIntServer(FakeStdioServer):
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
                                "serverInfo": {"name": "b", "version": "0"},
                            },
                        }
                        await self._to_client.put(json.dumps(reply).encode())
                        continue
                    if method == "notifications/initialized":
                        continue
                    if method == "tools/call":
                        payload = {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "result": {
                                "content": [
                                    {
                                        "type": "resource_link",
                                        "uri": file_url,
                                        "name": "a",
                                    }
                                ],
                                "isError": False,
                                "n": _OUT,
                            },
                        }
                        await self._to_client.put(json.dumps(payload).encode())
                        continue
                finally:
                    self._to_server.task_done()

    fetcher = RecordingFetcher({file_url: b"%PDF"})
    async with BigIntServer(default_handler) as server:
        client = make_client(make_config(), file_fetcher=fetcher)
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(IntegerDomainError, match=r"outside ±2\^53-1 domain"):
            await session.call_tool("get", {})
        assert fetcher.urls == []
        # Memory transport closed via _fail_close.
        assert server.client._closed is True  # noqa: SLF001


# --- 2) URL / DNS / httpx.InvalidURL --------------------------------------


def test_parse_host_port_invalid_port_is_ssrf_error() -> None:
    with pytest.raises(McpSsrfError, match="host/port parse failed"):
        parse_host_port("http://h:99999/")


def test_parse_host_port_broken_ipv6_is_ssrf_error() -> None:
    with pytest.raises(McpSsrfError, match="Invalid IPv6 URL"):
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
    from tests.mcp.test_ssrf import FakeHttp

    http = FakeHttp([])
    fetcher = _fetcher(http, resolver=FakeResolver({"h": ("8.8.8.8",)}))
    with pytest.raises(McpSsrfError, match="host/port parse failed"):
        await fetcher.fetch("http://h:99999/x", max_bytes=100, timeout_s=1.0)


@pytest.mark.asyncio
async def test_httpx_invalid_url_is_mcp_transport_error() -> None:
    transport = HttpxTransport()
    try:
        with pytest.raises(McpTransportError, match="httpx invalid URL"):
            await transport.open(
                "GET",
                "http://example.com/\n",
                headers={},
                body=None,
                timeout_s=1.0,
                connect_ip=None,
            )
    finally:
        await transport.aclose()


# --- 3) Typed JSON-RPC id equality ----------------------------------------


def test_json_rpc_ids_equal_is_type_sensitive() -> None:
    assert json_rpc_ids_equal(1, 1) is True
    assert json_rpc_ids_equal(1, 1.0) is False
    assert json_rpc_ids_equal(True, 1) is False


@pytest.mark.asyncio
async def test_session_rejects_float_id_for_int_request() -> None:
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
                        await self._to_client.put(json.dumps(reply).encode())
                        continue
                finally:
                    self._to_server.task_done()

    async with FloatIdServer(default_handler) as server:
        client = make_client(make_config(), file_fetcher=RecordingFetcher({}))
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(McpProtocolError, match="id does not match request"):
            await session.list_tools()


# --- 4) Deterministic connect cancel (event + process group dead) ---------


@pytest.mark.asyncio
async def test_connect_cancelled_during_initialize_closes_stdio(
    tmp_path: Path,
) -> None:
    ready_fifo = tmp_path / "ready.fifo"
    os.mkfifo(ready_fifo)
    script = (
        "import os, sys\n"
        f"fd = os.open({str(ready_fifo)!r}, os.O_WRONLY)\n"
        "os.write(fd, (str(os.getpid()) + ' ' + str(os.getpgid(0)) + '\\n').encode())\n"
        "os.close(fd)\n"
        "sys.stdin.readline()\n"
        "import time; time.sleep(3600)\n"
    )
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
    in_receive = asyncio.Event()

    async def wait_for(awaitable: Any, *, timeout_s: float) -> Any:
        # Hang on initialize response receive until cancelled.
        if not in_receive.is_set():
            in_receive.set()
            try:
                return await asyncio.Future()  # type: ignore[return-value]
            finally:
                if asyncio.iscoroutine(awaitable):
                    awaitable.close()
        return await awaitable

    client = make_client(
        cfg,
        file_fetcher=RecordingFetcher({}),
        clock=SystemClock(),
        sleep=system_sleep(),
        wait_for=wait_for,
    )

    def _blocking_ready_line() -> str:
        with open(ready_fifo, encoding="utf-8") as f:
            return f.readline().strip()

    ready_task = asyncio.create_task(asyncio.to_thread(_blocking_ready_line))
    task = asyncio.create_task(client.connect("slow-init"))
    await in_receive.wait()
    ready_text = await ready_task
    pid_s, pgid_s = ready_text.split()
    pgid = int(pgid_s)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # aclose/_reap_process_group returns only after the group is gone.
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)
    assert int(pid_s) > 0


# --- 5) stdio aclose ------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_aclose_sets_closed_only_after_reap() -> None:
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
    await transport.aclose()


@pytest.mark.asyncio
async def test_stdio_aclose_retry_after_cancel_during_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import coscientist.mcp.stdio as stdio_mod

    started = asyncio.Event()

    async def hanging_reap(*_a: Any, **_k: Any) -> None:
        started.set()
        await asyncio.Future()

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
    monkeypatch.undo()
    await transport.aclose()
    assert transport._closed is True  # noqa: SLF001


# --- 6) Session deadline from clock (N6) — wait_for does not advance ------


@pytest.mark.asyncio
async def test_session_deadline_uses_injected_clock_without_wait_for_advance() -> None:
    """session._read_until_response remaining<=0 uses FakeClock; wait_for is passthrough."""

    class NotifyFloodServer(FakeStdioServer):
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
                        # Flood notifications; never the matching response.
                        for _ in range(5):
                            note = {"jsonrpc": "2.0", "method": "notifications/message"}
                            await self._to_client.put(json.dumps(note).encode())
                        continue
                finally:
                    self._to_server.task_done()

    clock = FakeClock(mono=0.0)
    receives = {"n": 0}

    async def passthrough_wait_for(awaitable: Any, *, timeout_s: float) -> Any:
        # Do NOT advance clock — deadline logic must come from session.py:324-328.
        result = await awaitable
        receives["n"] += 1
        # After initialize receive, each notification receive advances clock.
        if receives["n"] >= 2:
            clock.advance(1.0)
        return result

    async with NotifyFloodServer(default_handler) as server:
        client = make_client(
            make_config(list_tools_timeout_s=2.5),
            file_fetcher=RecordingFetcher({}),
            clock=clock,
            sleep=fake_sleep(clock),
            wait_for=passthrough_wait_for,
        )
        session = await client.connect_memory("fake", server.client)
        with pytest.raises(
            McpTimeoutError,
            match="stdio/memory receive exceeded configured timeout",
        ):
            await session.list_tools()
        assert clock.monotonic() >= 2.5


# --- 7) Embedded resource -------------------------------------------------


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


# --- 8) fetch_files limits + file_timeout + multi-file budget (N7) --------


@pytest.mark.asyncio
async def test_fetch_files_enforces_max_files_and_total_bytes() -> None:
    class CountingFetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
            self.calls.append(url)
            return b"x" * 10

    async def wait_for(awaitable: Any, *, timeout_s: float) -> Any:
        return await awaitable

    fetcher = CountingFetcher()
    with pytest.raises(McpResponseLimitError, match="max_files_per_call") as ei:
        await fetch_files(
            ("https://a/1", "https://a/2", "https://a/3"),
            fetcher=fetcher,  # type: ignore[arg-type]
            max_bytes=100,
            max_files=2,
            max_total_bytes=1000,
            timeout_s=5.0,
            file_timeout_s=5.0,
            clock=SystemClock(),
            wait_for=wait_for,
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
            file_timeout_s=5.0,
            clock=SystemClock(),
            wait_for=wait_for,
        )
    assert ei2.value.limit == 15
    assert len(fetcher2.calls) == 2


@pytest.mark.asyncio
async def test_fetch_files_applies_min_of_remaining_and_file_timeout() -> None:
    seen: list[float] = []

    class CapturingFetcher:
        async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
            seen.append(timeout_s)
            return b"x"

    clock = FakeClock(mono=0.0)

    async def wait_for(awaitable: Any, *, timeout_s: float) -> Any:
        return await awaitable

    await fetch_files(
        ("https://a/1",),
        fetcher=CapturingFetcher(),  # type: ignore[arg-type]
        max_bytes=100,
        max_files=10,
        max_total_bytes=1000,
        timeout_s=10.0,
        file_timeout_s=1.5,
        clock=clock,
        wait_for=wait_for,
    )
    assert seen == [1.5]


@pytest.mark.asyncio
async def test_fetch_files_multi_file_budget_exhausted() -> None:
    """Overall files_fetch_timeout_s budget runs out across multiple files (N7)."""
    clock = FakeClock(mono=0.0)
    calls: list[str] = []

    class Fetcher:
        async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
            calls.append(url)
            return b"ok"

    async def wait_for(awaitable: Any, *, timeout_s: float) -> Any:
        # First file consumes the whole overall budget.
        clock.advance(1.0)
        return await awaitable

    with pytest.raises(McpTimeoutError, match="files_fetch_timeout_s"):
        await fetch_files(
            ("https://a/1", "https://a/2"),
            fetcher=Fetcher(),  # type: ignore[arg-type]
            max_bytes=100,
            max_files=10,
            max_total_bytes=1000,
            timeout_s=1.0,
            file_timeout_s=5.0,
            clock=clock,
            wait_for=wait_for,
        )
    assert calls == ["https://a/1"]


def test_config_requires_files_limits_without_defaults() -> None:
    with pytest.raises(ValidationError, match="Field required"):
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


# --- 9) httpx only in httpx_transport (N5: path from __file__) ------------


def test_httpx_only_imported_from_httpx_transport() -> None:
    import ast

    mcp_root = Path(__file__).resolve().parents[2] / "coscientist" / "mcp"
    py_files = list(mcp_root.rglob("*.py"))
    assert py_files, f"no Python files under {mcp_root}"
    offenders: list[str] = []
    for path in py_files:
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


# --- 11) precise version negotiation -------------------------------------


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
