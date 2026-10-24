"""MCP client configuration. Every operational value is required (no guessed defaults)."""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StdioServerConfig(BaseModel):
    """Launch a local MCP server as a stdio subprocess (own process group)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: Literal["stdio"]
    name: str = Field(min_length=1)
    command: str = Field(min_length=1)
    args: tuple[str, ...]
    # Names of environment variables to copy into the child (values from os.environ).
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
    # Required field; pass None explicitly when the server needs no bearer token.
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
    client_name: str = Field(min_length=1)
    client_version: str = Field(min_length=1)
    connect_timeout_s: float
    list_tools_timeout_s: float
    call_tool_timeout_s: float
    terminate_grace_s: float
    kill_grace_s: float
    max_response_bytes: int = Field(ge=1)
    max_file_bytes: int = Field(ge=1)
    file_fetch_timeout_s: float
    # Schemes the client may download when a typed MCP URI field references a file.
    fetch_uri_schemes: tuple[str, ...]

    @field_validator(
        "connect_timeout_s",
        "list_tools_timeout_s",
        "call_tool_timeout_s",
        "terminate_grace_s",
        "kill_grace_s",
        "file_fetch_timeout_s",
    )
    @classmethod
    def _positive_finite(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0.0:
            raise ValueError("timeout/grace must be finite and > 0")
        return v

    @field_validator("fetch_uri_schemes")
    @classmethod
    def _schemes(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("fetch_uri_schemes must be non-empty")
        for scheme in v:
            if not scheme or scheme != scheme.lower() or any(ch.isspace() for ch in scheme):
                raise ValueError("fetch_uri_schemes entries must be non-empty lowercase scheme names")
        return v

    @model_validator(mode="after")
    def _unique_server_names(self) -> McpClientConfig:
        names = [s.name for s in self.servers]
        if len(names) != len(set(names)):
            raise ValueError("server names in profile must be unique")
        return self
