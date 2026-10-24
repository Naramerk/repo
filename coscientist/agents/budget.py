"""Attempt/role budget accounting for the agent layer (fail-closed)."""

from __future__ import annotations

from decimal import Decimal

from coscientist.agents.config import AgentBudgetLimits
from coscientist.agents.errors import AgentBudgetExceeded


class BudgetTracker:
    """Tracks spend against required ``AgentBudgetLimits`` (no silent clamps)."""

    def __init__(self, limits: AgentBudgetLimits) -> None:
        self.limits = limits
        self.llm_calls = 0
        self.tool_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.latency_ms = 0.0
        self.cost_rub = Decimal("0")

    def _tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def check_can_start_llm_call(self) -> None:
        if self.llm_calls + 1 > self.limits.max_llm_calls:
            raise AgentBudgetExceeded(
                "attempt",
                f"max_llm_calls={self.limits.max_llm_calls} would be exceeded",
            )

    def check_llm_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cost_rub: Decimal | None,
    ) -> None:
        next_tokens = self._tokens() + prompt_tokens + completion_tokens
        if next_tokens > self.limits.max_tokens:
            raise AgentBudgetExceeded(
                "attempt",
                f"max_tokens={self.limits.max_tokens} would be exceeded",
            )
        if self.limits.max_cost_rub is not None:
            if cost_rub is None:
                raise AgentBudgetExceeded(
                    "attempt",
                    "max_cost_rub is set but call cost_rub is null (cost_unverified)",
                )
            if self.cost_rub + cost_rub > self.limits.max_cost_rub:
                raise AgentBudgetExceeded(
                    "attempt",
                    f"max_cost_rub={self.limits.max_cost_rub} would be exceeded",
                )

    def record_llm_call(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cost_rub: Decimal | None,
        latency_ms: float,
    ) -> None:
        self.check_can_start_llm_call()
        self.check_llm_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_rub=cost_rub,
        )
        self.llm_calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.latency_ms += latency_ms
        if cost_rub is not None:
            self.cost_rub += cost_rub

    def check_tool_call(self) -> None:
        if self.tool_calls + 1 > self.limits.max_tool_calls:
            raise AgentBudgetExceeded(
                "attempt",
                f"max_tool_calls={self.limits.max_tool_calls} would be exceeded",
            )

    def record_tool_call(self) -> None:
        self.check_tool_call()
        self.tool_calls += 1
