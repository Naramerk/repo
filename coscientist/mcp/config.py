"""MCP client configuration. Every operational value is required (no guessed defaults)."""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from coscientist.mcp.schemes import HARD_FETCH_SCHEMES


def _identifier(value: str) -> str:
    if not value:
        raise ValueError("must be non-empty")
    if any(ch.isspace() for ch in value):
        raise ValueError(f"must not contain whitespace: {value!r}")
    return value


def _executable(value: str) -> str:
    """Path/command token: non-empty; no leading/trailing WS; inner spaces OK."""
    if not value:
        raise ValueError("must be non-empty")
    if value != value.strip():
        raise ValueError(f"must not have leading/trailing whitespace: {value!r}")
    return value


Identifier = Annotated[str, AfterValidator(_identifier)]
Executable = Annotated[str, AfterValidator(_executable)]


class StdioServerConfig(BaseModel):
    """Launch a local MCP server as a stdio subprocess (own process group)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    transport: Literal["stdio"]
    name: Identifier
    command: Executable
    args: tuple[str, ...]
    env_keys: tuple[Identifier, ...]


class StreamableHttpServerConfig(BaseModel):
    """Connect to a remote MCP server over Streamable HTTP."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    transport: Literal["streamable_http"]
    name: Identifier
    url: str = Field(min_length=1)
    api_key_env: Identifier | None

    @field_validator("url")
    @classmethod
    def _url_no_whitespace(cls, v: str) -> str:
        if any(ch.isspace() for ch in v):
            raise ValueError("url must not contain whitespace")
        return v


McpServerConfig = Annotated[
    StdioServerConfig | StreamableHttpServerConfig,
    Field(discriminator="transport"),
]


def unique_mcp_server_names(
    servers: tuple[McpServerConfig, ...],
) -> tuple[McpServerConfig, ...]:
    names = [s.name for s in servers]
    if len(names) != len(set(names)):
        raise ValueError("mcp server names must be unique")
    return servers


McpServers = Annotated[
    tuple[McpServerConfig, ...],
    AfterValidator(unique_mcp_server_names),
]


class McpClientConfig(BaseModel):
    """Explicit server profile plus timeouts/limits. Servers are never discovered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    servers: McpServers
    protocol_version: str = Field(min_length=1)
    supported_protocol_versions: tuple[str, ...]
    client_name: str = Field(min_length=1)
    client_version: str = Field(min_length=1)
    connect_timeout_s: float
    list_tools_timeout_s: float
    call_tool_timeout_s: float
    notification_timeout_s: float
    terminate_grace_s: float
    kill_grace_s: float
    process_group_poll_interval_s: float
    max_response_bytes: int = Field(ge=1)
    max_file_bytes: int = Field(ge=1)
    file_fetch_timeout_s: float
    max_files_per_call: int = Field(ge=1)
    max_files_total_bytes: int = Field(ge=1)
    files_fetch_timeout_s: float
    fetch_uri_schemes: tuple[str, ...]
    max_redirect_hops: int = Field(ge=0)
    max_list_tools_pages: int = Field(ge=1)
    stdio_read_chunk_bytes: int = Field(ge=1)
    http_read_chunk_bytes: int = Field(ge=1)
    max_stderr_bytes: int = Field(ge=1)
    http_request_ok_statuses: tuple[int, ...]
    http_notification_ok_statuses: tuple[int, ...]
    allowed_file_content_types: tuple[str, ...]
    allowed_content_encodings: tuple[str, ...]

    @field_validator(
        "connect_timeout_s",
        "list_tools_timeout_s",
        "call_tool_timeout_s",
        "notification_timeout_s",
        "terminate_grace_s",
        "kill_grace_s",
        "process_group_poll_interval_s",
        "file_fetch_timeout_s",
        "files_fetch_timeout_s",
    )
    @classmethod
    def _positive_finite(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0.0:
            raise ValueError("timeout/grace/interval must be finite and > 0")
        return v

    @field_validator("fetch_uri_schemes")
    @classmethod
    def _schemes(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("fetch_uri_schemes must be non-empty")
        for scheme in v:
            if scheme not in HARD_FETCH_SCHEMES:
                raise ValueError(f"fetch_uri_schemes entry {scheme!r} hard-denied; only http/https allowed")
        return v

    @field_validator("supported_protocol_versions")
    @classmethod
    def _versions(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("supported_protocol_versions must be non-empty")
        for ver in v:
            if not ver or any(ch.isspace() for ch in ver):
                raise ValueError("supported_protocol_versions entries must be non-empty without whitespace")
        return v

    @field_validator("http_request_ok_statuses", "http_notification_ok_statuses")
    @classmethod
    def _statuses(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if not v:
            raise ValueError("HTTP status tuple must be non-empty")
        for code in v:
            if not (100 <= code <= 599):
                raise ValueError(f"HTTP status out of range: {code}")
        return v

    @field_validator("allowed_file_content_types")
    @classmethod
    def _content_types(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("allowed_file_content_types must be non-empty")
        for ct in v:
            if not ct or ct != ct.lower() or any(ch.isspace() for ch in ct):
                raise ValueError("content types must be non-empty lowercase tokens without whitespace")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> McpClientConfig:
        if self.protocol_version not in self.supported_protocol_versions:
            raise ValueError("protocol_version must be listed in supported_protocol_versions")
        return self
