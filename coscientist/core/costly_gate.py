"""HITL gate costly_or_nonreal — runtime mirror of tests/refmodel.costly_gate."""

from __future__ import annotations

from typing import Any, Literal

Answer = Literal["approve", "reject", "delegate"] | None
Effect = str


def costly_gate(
    mode: str,
    answer: Answer,
    cost_rub: float = 0.0,
    budget_left_rub: float = 0.0,
    critic_accepts: bool = True,
) -> tuple[Effect, dict[str, Any]]:
    """Resolve costly_or_nonreal. answer: approve | reject | delegate | None (timeout).

    Returns (task_effect, event) where event always carries decided_by, reason and spent_rub.
    """
    if answer in ("approve", "reject"):
        eff = "RUN" if answer == "approve" else "SKIPPED(human_rejected)"
        return eff, {"decided_by": "human", "reason": answer, "spent_rub": 0.0}
    delegated_by = "human" if answer == "delegate" else "timeout"
    if mode == "fail":
        if answer == "delegate":
            return (
                "WAIT",
                {
                    "decided_by": None,
                    "reason": "delegation_disabled",
                    "spent_rub": 0.0,
                },
            )
        return (
            "FAILED(human_timeout)",
            {
                "decided_by": None,
                "reason": "human_timeout",
                "spent_rub": 0.0,
            },
        )
    if mode == "delegate_capped" and cost_rub > budget_left_rub:
        return (
            "SKIPPED(budget)",
            {
                "decided_by": "critic",
                "delegated_by": delegated_by,
                "reason": "delegated_costly_exhausted",
                "spent_rub": 0.0,
            },
        )
    if mode not in ("delegate_capped", "delegate"):
        raise ValueError(mode)
    eff = "RUN" if critic_accepts else "SKIPPED(critic_rejected)"
    spent = float(cost_rub) if critic_accepts else 0.0
    return (
        eff,
        {
            "decided_by": "critic",
            "delegated_by": delegated_by,
            "reason": f"critic:{mode}",
            "spent_rub": spent,
        },
    )
