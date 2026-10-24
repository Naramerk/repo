"""Costly_or_nonreal budget gate (DESIGN §7.3 / RESEARCH_MODEL §5.2).

Experimental / pre-M2: not exported from ``coscientist.core``.
"""

from __future__ import annotations

from typing import Any


class DelegatedBudget:
    """Budget(delegated_costly), ₽: stateful counter; actual spend is debited after each attempt."""

    def __init__(self, limit_rub: float) -> None:
        self.limit_rub, self.spent_rub, self.blocked = float(limit_rub), 0.0, False

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
        b = cls(state["limit_rub"])
        b.spent_rub = float(state.get("spent_rub") or 0.0)
        b.blocked = bool(state.get("blocked"))
        return b


def costly_gate(
    mode: str,
    answer: str | None,
    cost_estimate_rub: float = 0.0,
    budget: DelegatedBudget | None = None,
    critic_accepts: bool = True,
):
    """HITL gate costly_or_nonreal. answer: approve | reject | delegate | None (timeout).

    ``critic_accepts`` is a **stub**: M0/experimental has no real critic agent.
    Callers pass True/False to simulate accept vs reject; wiring to the critic
    role lands in M2. See docs/DESIGN.md §15 M0 notes.
    """
    if answer in ("approve", "reject"):
        eff = "RUN" if answer == "approve" else "SKIPPED(human_rejected)"
        return eff, {
            "decided_by": "human",
            "delegated_by": None,
            "reason": answer,
            "spent_rub": 0.0,
        }
    if mode not in ("fail", "delegate_capped", "delegate"):
        raise ValueError(mode)
    delegated_by = "human" if answer == "delegate" else "timeout"
    if mode == "fail" and answer is None:
        return (
            "FAILED(human_timeout)",
            {
                "decided_by": None,
                "delegated_by": None,
                "reason": "human_timeout",
                "spent_rub": 0.0,
            },
        )
    if mode != "delegate":
        why = "нет бюджета" if budget is None else budget.refuse_reason(cost_estimate_rub)
        if why:
            ev = {
                "decided_by": None,
                "delegated_by": delegated_by,
                "spent_rub": 0.0,
                "human_message": why,
            }
            if mode == "fail":
                return "WAIT", {**ev, "reason": "delegation_disabled"}
            return (
                "SKIPPED(budget)",
                {
                    **ev,
                    "decided_by": "critic",
                    "reason": "delegated_costly_exhausted",
                },
            )
    # stub: critic_accepts stands in for a real critic verdict (M2)
    eff = "RUN" if critic_accepts else "SKIPPED(critic_rejected)"
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
    if budget is not None and event.get("decided_by") == "critic":
        budget.debit(actual_rub, event.get("cost_estimate_rub", 0.0))
    event["spent_rub"] = actual_rub
    return event
