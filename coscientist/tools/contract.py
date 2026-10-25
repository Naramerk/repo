"""ToolSpec / ToolError / ToolOutput (DESIGN §6.1 + DESIGN_M1 §8 effect class).

``ToolSpec.effect`` is advisory for catalog authors only. ``invoke_tool`` takes
the authoritative effect from ``coscientist.core.execution.tool_registry`` (unregistered
→ ``external_effect``). Direct ``Tool.call`` bypasses gates — do not use it
outside tests; agent wiring must go through ``invoke_tool``.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from coscientist.core.execution.domain import Artifact
from coscientist.tools.call_ctx import CallCtx as CallCtx

ToolEffect = Literal["read", "write", "external_effect"]
ToolErrorKind = Literal["local", "transport_unsent", "transport_sent", "opaque"]
ToolProvider = Literal["mcp", "local", "coder", "alembic"]


class ToolSpec(BaseModel):
    """Catalog draft. Runtime effect is resolved via the closed core registry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    provider: ToolProvider = "local"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    # Advisory only — register the same name in core.tool_registry for gating.
    effect: ToolEffect = "read"
    # Advisory: real gate is RegisteredToolSpec.is_fetch in the closed registry.
    is_fetch: bool = False
    idempotent: bool = False
    supports_idem_key: bool = False
    timeout_s: float = 120
    max_retries: int = 2


class ToolError(Exception):
    """Typed tool failure (DESIGN §6.1 / §6.2)."""

    def __init__(
        self,
        kind: ToolErrorKind,
        detail: str,
        *,
        retryable: bool | None = None,
    ) -> None:
        self.kind = kind
        self.detail = detail
        if retryable is None:
            retryable = kind == "transport_unsent"
        self.retryable = bool(retryable)
        super().__init__(f"ToolError({kind}): {detail}")


class ToolOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data: Any = None
    artifacts: list[Artifact] = Field(default_factory=list)


class Tool(Protocol):
    spec: ToolSpec

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput: ...
