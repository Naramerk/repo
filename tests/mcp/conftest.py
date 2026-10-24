"""Shared fixtures / helpers for coscientist.mcp tests."""

from __future__ import annotations

import sys
from typing import Any

from coscientist.mcp import McpClientConfig, StdioServerConfig


def make_config(
    *,
    servers: tuple[Any, ...] | None = None,
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
