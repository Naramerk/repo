"""Plan Need helpers (G4). ``Need`` is in domain; resolve/validate live here."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from coscientist.core.execution.domain import Need

NeedKind = Literal["hypotheses", "literature"]


class PlanNeedRefuse(ValueError):
    """Typed refuse for Need contradictions (not a silent fix)."""


def validate_need(need: Need | dict[str, Any]) -> Need:
    """Parse/validate a Need; raise on empty reason or bad shape."""
    if isinstance(need, Need):
        return Need.model_validate(need.model_dump())
    return Need.model_validate(need)


def need_from_plan_blob(prev: Mapping[str, Any] | None, field: NeedKind) -> Need:
    """Carry Need from active plan blob. Missing after upcast = corruption → refuse.

    ``legacy_absent`` is upcaster-only (events layer); never synthesized here.
    """
    if prev is None or field not in prev or prev[field] is None:
        raise PlanNeedRefuse(f"plan_need_corrupt:missing_{field}")
    return validate_need(prev[field])


def resolve_plan_needs(
    *,
    hypotheses: Need | dict[str, Any] | None,
    literature: Need | dict[str, Any] | None,
) -> tuple[Need, Need]:
    """Resolve Plan Needs for accept_plan. Omitted → PlanNeedRefuse (planner decides)."""
    if hypotheses is None:
        raise PlanNeedRefuse("plan_need_required:hypotheses")
    if literature is None:
        raise PlanNeedRefuse("plan_need_required:literature")
    return validate_need(hypotheses), validate_need(literature)
