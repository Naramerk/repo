"""Typed MCP request/response models for the thin client."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class McpTool(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str | None
    input_schema: dict[str, Any]


class FetchedFile(BaseModel):
    """File downloaded during a call because an MCP reply referenced an expiring URL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    raw_bytes: bytes
    source_uri: str = Field(min_length=1)


class McpReply(BaseModel):
    """Every successful MCP reply: JCS content digest + raw wire bytes + optional files."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content_digest: str = Field(min_length=1)
    raw_bytes: bytes
    result: dict[str, Any]
    files: tuple[FetchedFile, ...]


class ListToolsReply(McpReply):
    tools: tuple[McpTool, ...]


class CallToolReply(McpReply):
    is_error: bool
