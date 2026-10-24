"""Costly_or_nonreal budget gate (DESIGN §7.3 / RESEARCH_MODEL §5.2).

Experimental / pre-M2: not exported from ``coscientist.core``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class CostlyEffect(str, Enum):
    """Structured gate outcome — callers must match on this, not string prefixes."""

    RUN = "RUN"
    WAIT = "WAIT"
    FAILED_HUMAN_TIMEOUT = "FAILED(human_timeout)"
    SKIPPED_BUDGET = "SKIPPED(budget)"
    SKIPPED_HUMAN_REJECTED = "SKIPPED(human_rejected)"
    SKIPPED_CRITIC_REJECTED = "SKIPPED(critic_rejected)"


def _require_float(value: object, *, what: str) -> float:
    if type(value) is not float and type(value) is not int:
        raise TypeError(f"{what} must be int|float, got {type(value).__name__}")
    return float(value)  # audit-ok[coerce_float]: coerce after explicit numeric type gate


def _require_bool(value: object, *, what: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{what} must be bool, got {type(value).__name__}")
    return value


class DelegatedBudget:
    """Budget(delegated_costly, max_llm_calls=100, max_tokens=1000000, max_tool_calls=500), ₽: stateful counter; actual spend is debited after each attempt."""  # noqa: E501

    def __init__(self, limit_rub: float) -> None:
        self.limit_rub = _require_float(limit_rub, what="limit_rub")
        self.spent_rub = 0.0
        self.blocked = False

    @property
    def remaining_rub(self) -> float:
        return max(self.limit_rub - self.spent_rub, 0.0)

    def refuse_reason(self, estimate_rub: float) -> str | None:
        if self.blocked:
            return "перерасход: делегирование заблокировано"
        if estimate_rub > self.remaining_rub:
            return "превышает остаток"
        return None

    def debit(self, actual_rub: float, estimate_rub: float) -> float:
        self.spent_rub += actual_rub
        if actual_rub > estimate_rub:
            self.blocked = True
        return actual_rub

    def to_state(self) -> dict[str, Any]:
        return {
            "limit_rub": self.limit_rub,
            "spent_rub": self.spent_rub,
            "blocked": self.blocked,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> DelegatedBudget:
        if "limit_rub" not in state:
            raise KeyError("DelegatedBudget state missing limit_rub")
        b = cls(state["limit_rub"])
        if "spent_rub" not in state:
            raise KeyError("DelegatedBudget state missing spent_rub")
        if "blocked" not in state:
            raise KeyError("DelegatedBudget state missing blocked")
        b.spent_rub = _require_float(state["spent_rub"], what="spent_rub")
        b.blocked = _require_bool(state["blocked"], what="blocked")
        return b


def costly_gate(
    mode: str,
    answer: str | None,
    cost_estimate_rub: float,
    budget: DelegatedBudget | None,
    critic_accepts: bool,
) -> tuple[CostlyEffect, dict[str, Any]]:
    """HITL gate costly_or_nonreal. answer: approve | reject | delegate | None (timeout).

    ``critic_accepts`` is a **stub**: M0/experimental has no real critic agent.
    Callers pass True/False to simulate accept vs reject; wiring to the critic
    role lands in M2. See docs/DESIGN.md §15 M0 notes.

    Returns ``(CostlyEffect, event_dict)``. Unknown outcomes raise.
    """
    if type(cost_estimate_rub) is not float and type(cost_estimate_rub) is not int:
        raise TypeError(
            f"cost_estimate_rub must be int|float, got {type(cost_estimate_rub).__name__}"
        )
    if type(critic_accepts) is not bool:
        raise TypeError(
            f"critic_accepts must be bool, got {type(critic_accepts).__name__}"
        )
    if answer in ("approve", "reject"):
        eff = (
            CostlyEffect.RUN
            if answer == "approve"
            else CostlyEffect.SKIPPED_HUMAN_REJECTED  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
        )
        return eff, {
            "decided_by": "human",
            "delegated_by": None,
            "reason": answer,
            "spent_rub": 0.0,
        }
    if mode not in ("fail", "delegate_capped", "delegate"):
        raise ValueError(mode)
    if answer == "delegate":
        delegated_by = "human"
    elif answer is None:
        delegated_by = "timeout"
    else:
        raise ValueError(f"costly_gate unexpected answer={answer!r}")
    if mode == "fail" and answer is None:
        return (
            CostlyEffect.FAILED_HUMAN_TIMEOUT,
            {
                "decided_by": None,
                "delegated_by": None,
                "reason": "human_timeout",
                "spent_rub": 0.0,
            },
        )
    if mode != "delegate":
        why: str | None
        if budget is None:
            why = "нет бюджета"
        else:
            why = budget.refuse_reason(cost_estimate_rub)
        if why:
            ev = {
                "decided_by": None,
                "delegated_by": delegated_by,
                "spent_rub": 0.0,
                "human_message": why,
            }
            if mode == "fail":
                return CostlyEffect.WAIT, {**ev, "reason": "delegation_disabled"}
            return (
                CostlyEffect.SKIPPED_BUDGET,
                {
                    **ev,
                    "decided_by": "critic",
                    "reason": "delegated_costly_exhausted",
                },
            )
    # stub: critic_accepts stands in for a real critic verdict (M2)
    eff = (
        CostlyEffect.RUN
        if critic_accepts
        else CostlyEffect.SKIPPED_CRITIC_REJECTED  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
    )
    return (
        eff,
        {
            "decided_by": "critic",
            "delegated_by": delegated_by,
            "reason": f"critic:{mode}",
            "spent_rub": 0.0,
            "cost_estimate_rub": cost_estimate_rub,
        },
    )


def settle(event: dict[str, Any], budget: DelegatedBudget | None, actual_rub: float):
    """After the attempt: debit actual spend (if budgeted) and record it for the report."""
    if budget is not None and event.get("decided_by") == "critic":  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
        budget.debit(actual_rub, event["cost_estimate_rub"])
    event["spent_rub"] = actual_rub
    return event
