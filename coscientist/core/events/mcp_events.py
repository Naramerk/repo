"""McpRequestRecorded / McpResponseRecorded payloads (MCP-J1 / #78).

Validated at emit; projection fold is graph G18. No silent defaults.
Owner path: ``tools.mcp_adapter`` journals these around ``McpSession.call_tool``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

McpEventType = Literal["McpRequestRecorded", "McpResponseRecorded"]


def require_nonblank(value: str, *, field: str) -> str:
    if value.strip() != value or not value.strip():
        raise ValueError(f"{field} must be non-empty without surrounding whitespace")
    return value


class _McpCallPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    call_id: str = Field(min_length=1)
    server_name: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)

    @field_validator("call_id", "server_name", "tool", "attempt_id")
    @classmethod
    def _non_blank(cls, v: str, info: ValidationInfo) -> str:
        return require_nonblank(v, field=str(info.field_name))


class McpRequestRecordedPayload(_McpCallPayload):
    """Wire shape for one MCP ``tools/call`` request (args by digest only)."""

    args_digest: str = Field(min_length=1)

    @field_validator("args_digest")
    @classmethod
    def _args_digest(cls, v: str, info: ValidationInfo) -> str:
        return require_nonblank(v, field=str(info.field_name))


class McpResponseRecordedPayload(_McpCallPayload):
    """Wire shape for one MCP ``tools/call`` reply (``CallToolReply`` digests)."""

    content_digest: str = Field(min_length=1)
    is_error: bool

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, v: str, info: ValidationInfo) -> str:
        return require_nonblank(v, field=str(info.field_name))
