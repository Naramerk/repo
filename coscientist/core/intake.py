"""Study intake: Accept or Refuse before SPEC / stage tasks (PR 4)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from coscientist.core.domain import Budget
from coscientist.core.reasons import RefusalKind


@dataclass(frozen=True)
class IntakeAccept:
    """Intake passed; create_study may emit stage tasks."""


@dataclass(frozen=True)
class IntakeRefuse:
    kind: RefusalKind
    reason: str


IntakeResult = IntakeAccept | IntakeRefuse


class SafetyPort(Protocol):
    """Safety/scope classifier behind a port (may time out)."""

    def classify(self, request: str, inputs: list[Any]) -> RefusalKind | None:
        """Return SAFETY / OUT_OF_SCOPE, or None if the request is allowed."""


def _budget_too_low(budget: Budget | None, min_plan_cost: Decimal) -> bool:
    if budget is None:
        return False
    if budget.max_llm_calls <= 0:
        return True
    if budget.max_cost_rub is not None and budget.max_cost_rub <= 0:
        return True
    if min_plan_cost > 0 and budget.max_cost_rub is not None:
        if budget.max_cost_rub < min_plan_cost:
            return True
    return False


def intake(
    request: str,
    inputs: list[Any],
    profile: str,
    budget: Budget | None,
    *,
    min_plan_cost: Decimal | float | int = 0,
    safety: SafetyPort | None = None,
    safety_timeout_s: float = 5.0,
) -> IntakeResult:
    """Code checks + optional safety/scope port. Pure; no store I/O.

    Duration ≤ finalize_reserve_s stays a ``ValueError`` in ``create_study``
    (no ``RefusalKind`` for it). Profile is accepted for the port / future use.
    """
    _ = profile
    if not str(request or "").strip():
        return IntakeRefuse(RefusalKind.EMPTY_REQUEST, "empty request")
    min_cost = Decimal(str(min_plan_cost))
    if _budget_too_low(budget, min_cost):
        return IntakeRefuse(RefusalKind.BUDGET_ZERO, "budget below minimum")
    if safety is not None:
        try:
            # Port owns its timeout; TimeoutError → treat as safety refuse.
            kind = safety.classify(request, list(inputs or []))
        except TimeoutError:
            return IntakeRefuse(RefusalKind.SAFETY, "classifier_timeout")
        except Exception as e:  # noqa: BLE001 — port failure must not crash create
            return IntakeRefuse(RefusalKind.SAFETY, f"classifier_error:{type(e).__name__}")
        if kind in (RefusalKind.SAFETY, RefusalKind.OUT_OF_SCOPE):
            return IntakeRefuse(kind, f"classifier:{kind.value}")
        if kind is not None:
            return IntakeRefuse(kind, f"classifier:{kind.value}")
    _ = safety_timeout_s
    return IntakeAccept()
