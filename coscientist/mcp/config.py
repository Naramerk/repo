"""MCP client configuration. Every operational value is required (no guessed defaults)."""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coscientist.mcp.schemes import HARD_FETCH_SCHEMES


class StdioServerConfig(BaseModel):
    """Launch a local MCP server as a stdio subprocess (own process group)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: Literal["stdio"]
    name: str = Field(min_length=1)
    command: str = Field(min_length=1)
    args: tuple[str, ...]
    env_keys: tuple[str, ...]

    @field_validator("name", "command")
    @classmethod
    def _no_surrounding_whitespace(cls, v: str) -> str:
        if v != v.strip():
            raise ValueError("must not have leading/trailing whitespace")
        return v

    @field_validator("env_keys")
    @classmethod
    def _env_key_names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for key in v:
            if not key or key != key.strip() or any(ch.isspace() for ch in key):
                raise ValueError(f"env_keys entry must be a non-empty name without whitespace: {key!r}")
        return v


class StreamableHttpServerConfig(BaseModel):
    """Connect to a remote MCP server over Streamable HTTP."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: Literal["streamable_http"]
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    api_key_env: str | None

    @field_validator("name", "url")
    @classmethod
    def _no_surrounding_whitespace(cls, v: str) -> str:
        if v != v.strip():
            raise ValueError("must not have leading/trailing whitespace")
        return v

    @field_validator("url")
    @classmethod
    def _url_no_whitespace(cls, v: str) -> str:
        if any(ch.isspace() for ch in v):
            raise ValueError("url must not contain whitespace")
        return v

    @field_validator("api_key_env")
    @classmethod
    def _api_key_env_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v != v.strip() or any(ch.isspace() for ch in v) or v == "":
            raise ValueError("api_key_env must be an environment variable name without whitespace")
        return v


McpServerConfig = Annotated[
    StdioServerConfig | StreamableHttpServerConfig,
    Field(discriminator="transport"),
]


class McpClientConfig(BaseModel):
    """Explicit server profile plus timeouts/limits. Servers are never discovered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    servers: tuple[McpServerConfig, ...]
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
        names = [s.name for s in self.servers]
        if len(names) != len(set(names)):
            raise ValueError("server names in profile must be unique")
        if self.protocol_version not in self.supported_protocol_versions:
            raise ValueError("protocol_version must be listed in supported_protocol_versions")
        return self
