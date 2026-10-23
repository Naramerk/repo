"""Study intake: Accept or Refuse before SPEC / stage tasks (PR 4)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from coscientist.core.domain import Budget
from coscientist.core.reasons import RefusalKind

REASON_MAX = 200


@dataclass(frozen=True)
class IntakeAccept:
    """Intake passed; create_study may emit stage tasks."""


@dataclass(frozen=True)
class IntakeRefuse:
    kind: RefusalKind
    reason: str
    classifier_error: bool = False


IntakeResult = IntakeAccept | IntakeRefuse


class SafetyPort(Protocol):
    """Safety/scope classifier behind a port (may time out)."""

    def classify(self, request: str, inputs: list[Any]) -> RefusalKind | None:
        """Return SAFETY / OUT_OF_SCOPE, or None if the request is allowed."""


def _clip_reason(reason: str) -> str:
    return str(reason)[:REASON_MAX]


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


def _refuse(
    kind: RefusalKind, reason: str, *, classifier_error: bool = False
) -> IntakeRefuse:
    return IntakeRefuse(kind, _clip_reason(reason), classifier_error=classifier_error)


async def _run_classifier(
    safety: SafetyPort,
    request: str,
    inputs: list[Any],
    *,
    timeout_s: float,
) -> IntakeRefuse | RefusalKind | None:
    """Run sync classify off the event loop under a wall-clock timeout."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(safety.classify, request, list(inputs)),
            timeout=timeout_s,
        )
    except TimeoutError:
        return _refuse(
            RefusalKind.SAFETY, "classifier_timeout", classifier_error=True
        )
    except Exception as e:  # noqa: BLE001 — port failure must not crash create
        return _refuse(
            RefusalKind.SAFETY,
            f"classifier_error:{type(e).__name__}",
            classifier_error=True,
        )


async def intake(
    request: str,
    inputs: list[Any],
    profile: str,
    budget: Budget | None,
    *,
    min_plan_cost: Decimal | float | int = 0,
    safety: SafetyPort | None = None,
    safety_timeout_s: float = 5.0,
    duration_s: float | None = None,
    finalize_reserve_s: float | None = None,
) -> IntakeResult:
    """Code checks + optional safety/scope port. No store I/O.

    ``duration_s <= finalize_reserve_s`` → ``DURATION_TOO_SHORT``. Classifier
    runs via ``asyncio.to_thread`` + ``wait_for`` so a hung port cannot block
    the event loop past ``safety_timeout_s``.
    """
    _ = profile
    if not str(request or "").strip():
        return _refuse(RefusalKind.EMPTY_REQUEST, "empty request")
    if (
        duration_s is not None
        and finalize_reserve_s is not None
        and duration_s <= finalize_reserve_s
    ):
        return _refuse(
            RefusalKind.DURATION_TOO_SHORT,
            f"duration ({duration_s}s) must exceed finalize_reserve_s "
            f"({finalize_reserve_s}s)",
        )
    min_cost = Decimal(str(min_plan_cost))
    if _budget_too_low(budget, min_cost):
        return _refuse(RefusalKind.BUDGET_ZERO, "budget below minimum")
    if safety is not None:
        result = await _run_classifier(
            safety, request, list(inputs or []), timeout_s=safety_timeout_s
        )
        if isinstance(result, IntakeRefuse):
            return result
        if result is not None:
            return _refuse(result, f"classifier:{result.value}")
    return IntakeAccept()
