"""Shared fixtures / helpers for coscientist.mcp tests."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable
from typing import Any, Mapping, TypeVar

from coscientist.mcp import McpClient, McpClientConfig, StdioServerConfig
from coscientist.mcp.config import McpServerConfig
from coscientist.mcp.runtime import AsyncSleep, AsyncWaitFor, Clock

T = TypeVar("T")


class _BufStream:
    """In-memory ``HttpStream`` with a canned status / headers / body."""

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


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Deterministic monotonic clock for timeout tests (no wall sleeps)."""

    def __init__(self, mono: float = 0.0) -> None:
        self._mono = mono

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._mono += seconds


def system_sleep() -> AsyncSleep:
    async def sleep(delay_s: float) -> None:
        await asyncio.sleep(delay_s)

    return sleep


def system_wait_for() -> AsyncWaitFor:
    async def wait_for(awaitable: Awaitable[T], *, timeout_s: float) -> T:
        return await asyncio.wait_for(awaitable, timeout=timeout_s)

    return wait_for


def fake_sleep(clock: FakeClock) -> AsyncSleep:
    async def sleep(delay_s: float) -> None:
        clock.advance(delay_s)
        await asyncio.sleep(0)

    return sleep


def fake_wait_for(clock: FakeClock, *, force_timeout: bool = False) -> AsyncWaitFor:
    async def wait_for(awaitable: Awaitable[T], *, timeout_s: float) -> T:
        if force_timeout:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            clock.advance(timeout_s)
            raise TimeoutError()
        task = asyncio.ensure_future(awaitable)
        try:
            return await asyncio.wait_for(task, timeout=timeout_s)
        except TimeoutError:
            clock.advance(timeout_s)
            raise

    return wait_for


def make_config(
    *,
    servers: tuple[McpServerConfig, ...] | None = None,
    max_response_bytes: int = 65536,
    max_file_bytes: int = 4096,
    call_tool_timeout_s: float = 2.0,
    list_tools_timeout_s: float = 2.0,
    connect_timeout_s: float = 2.0,
    notification_timeout_s: float = 2.0,
    terminate_grace_s: float = 0.5,
    kill_grace_s: float = 0.5,
    process_group_poll_interval_s: float = 0.05,
    file_fetch_timeout_s: float = 1.0,
    max_files_per_call: int = 32,
    max_files_total_bytes: int = 1_000_000,
    files_fetch_timeout_s: float = 5.0,
    fetch_uri_schemes: tuple[str, ...] = ("http", "https"),
    max_redirect_hops: int = 3,
    max_list_tools_pages: int = 32,
    stdio_read_chunk_bytes: int = 4096,
    http_read_chunk_bytes: int = 4096,
    max_stderr_bytes: int = 65536,
    http_request_ok_statuses: tuple[int, ...] = (200,),
    http_notification_ok_statuses: tuple[int, ...] = (202,),
    allowed_file_content_types: tuple[str, ...] = (
        "application/pdf",
        "application/octet-stream",
        "text/plain",
    ),
    allowed_content_encodings: tuple[str, ...] = (),
    protocol_version: str = "2025-06-18",
    supported_protocol_versions: tuple[str, ...] = ("2025-06-18", "2025-03-26"),
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
        protocol_version=protocol_version,
        supported_protocol_versions=supported_protocol_versions,
        client_name="coscientist-test",
        client_version="0.0.0",
        connect_timeout_s=connect_timeout_s,
        list_tools_timeout_s=list_tools_timeout_s,
        call_tool_timeout_s=call_tool_timeout_s,
        notification_timeout_s=notification_timeout_s,
        terminate_grace_s=terminate_grace_s,
        kill_grace_s=kill_grace_s,
        process_group_poll_interval_s=process_group_poll_interval_s,
        max_response_bytes=max_response_bytes,
        max_file_bytes=max_file_bytes,
        file_fetch_timeout_s=file_fetch_timeout_s,
        max_files_per_call=max_files_per_call,
        max_files_total_bytes=max_files_total_bytes,
        files_fetch_timeout_s=files_fetch_timeout_s,
        fetch_uri_schemes=fetch_uri_schemes,
        max_redirect_hops=max_redirect_hops,
        max_list_tools_pages=max_list_tools_pages,
        stdio_read_chunk_bytes=stdio_read_chunk_bytes,
        http_read_chunk_bytes=http_read_chunk_bytes,
        max_stderr_bytes=max_stderr_bytes,
        http_request_ok_statuses=http_request_ok_statuses,
        http_notification_ok_statuses=http_notification_ok_statuses,
        allowed_file_content_types=allowed_file_content_types,
        allowed_content_encodings=allowed_content_encodings,
    )


def make_client(
    config: McpClientConfig | None = None,
    *,
    clock: Clock | None = None,
    sleep: AsyncSleep | None = None,
    wait_for: AsyncWaitFor | None = None,
    **kwargs: Any,
) -> McpClient:
    """Build ``McpClient`` with system clock/sleep/wait_for unless overridden."""
    cfg = config if config is not None else make_config()
    return McpClient(
        cfg,
        clock=clock if clock is not None else SystemClock(),
        sleep=sleep if sleep is not None else system_sleep(),
        wait_for=wait_for if wait_for is not None else system_wait_for(),
        **kwargs,
    )
