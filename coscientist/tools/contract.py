"""ToolSpec / ToolError / ToolOutput (DESIGN §6.1 + DESIGN_M1 §8 effect class)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from coscientist.core.domain import Artifact, HitlPolicy

ToolEffect = Literal["read", "write", "external_effect"]
ToolErrorKind = Literal["local", "transport_unsent", "transport_sent", "opaque"]
ToolProvider = Literal["mcp", "local", "coder", "alembic"]


class ToolSpec(BaseModel):
    """Catalog entry for a tool. ``effect`` replaces DESIGN ``read_only`` (PR18)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    provider: ToolProvider = "local"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    effect: ToolEffect = "read"
    idempotent: bool = False
    # When True, recover may re-call after EffectIntended without EffectDone.
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
            # Default: only transport_unsent is retryable without further context.
            # transport_sent retryability depends on effect/idempotent at call site.
            retryable = kind == "transport_unsent"
        self.retryable = bool(retryable)
        super().__init__(f"ToolError({kind}): {detail}")


class ToolOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data: Any = None
    artifacts: list[Artifact] = Field(default_factory=list)


@dataclass
class CallCtx:
    """Invocation context for ``Tool.call`` / ``invoke_tool``."""

    study_id: str
    task_key: str
    attempt_id: str
    hitl_policy: HitlPolicy
    # True when any input/context artifact is tainted (blocks write tools).
    tainted_context: bool = False
    # Optional seq for ToolCallRef call_id = f"{attempt_id}:{call_seq}".
    call_seq: int = 1
    extra: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    spec: ToolSpec

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput: ...
