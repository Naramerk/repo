"""Typed I/O for the agent port (DESIGN §7.2)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coscientist.core.execution.domain import AgentTaskOutput, ToolCallRef


class ToolCallRequest(BaseModel):
    """One tool invocation proposed by the LLM (validated, not inferred)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class AgentDecision(BaseModel):
    """Structured LLM turn: either tool_calls or a final AgentTaskOutput, never both/neither."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_calls: tuple[ToolCallRequest, ...]
    final: AgentTaskOutput | None

    @model_validator(mode="after")
    def _xor(self) -> AgentDecision:
        has_tools = len(self.tool_calls) > 0
        has_final = self.final is not None
        if has_tools == has_final:
            raise ValueError(
                "AgentDecision requires exactly one of tool_calls (non-empty) or final"
            )
        return self


class AggregateUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0.0)
    cost_rub: Decimal | None
    llm_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)


class AgentRun(BaseModel):
    """Result of ``AgentPort.run`` (DESIGN §7.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output: AgentTaskOutput | None
    error: str | None
    usage: AggregateUsage
    tool_calls: tuple[ToolCallRef, ...]
    llm_call_ids: tuple[str, ...]
    status: Literal["ok", "budget_exceeded", "decision_invalid", "role_error", "error"]
