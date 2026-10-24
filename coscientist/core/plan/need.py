"""Plan Need helpers (G4). ``Need`` / ``NeedUnrecorded`` are in domain."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from coscientist.core.execution.domain import Need, NeedUnrecorded

NeedKind = Literal["hypotheses", "literature"]
PlanNeedField = Need | NeedUnrecorded


class PlanNeedRefuse(ValueError):
    """Typed refuse for Need contradictions (not a silent fix)."""


def validate_need(need: Need) -> Need:
    """Accept only a typed ``Need`` (dict parsing belongs on adapters)."""
    if not isinstance(need, Need):
        raise PlanNeedRefuse("plan_need_type:expected_Need")
    return need


def parse_plan_need(raw: Any) -> PlanNeedField:
    """Adapter: plan-blob / event dict → ``Need`` | ``NeedUnrecorded``."""
    if isinstance(raw, NeedUnrecorded):
        return raw
    if isinstance(raw, Need):
        return raw
    if not isinstance(raw, Mapping):
        raise PlanNeedRefuse("plan_need_corrupt:bad_shape")
    if "source_schema_version" in raw and "needed" not in raw:
        return NeedUnrecorded.model_validate(raw)
    return Need.model_validate(raw)


def require_need(value: PlanNeedField) -> Need:
    """Fail-closed at use: ``NeedUnrecorded`` is not a planner decision."""
    if isinstance(value, NeedUnrecorded):
        raise PlanNeedRefuse(f"plan_need_unrecorded:source_schema_version={value.source_schema_version}")
    return validate_need(value)


def need_from_plan_blob(prev: Mapping[str, Any] | None, field: NeedKind) -> PlanNeedField:
    """Carry Need from active plan blob. Missing after upcast = corruption → refuse."""
    if prev is None or field not in prev or prev[field] is None:
        raise PlanNeedRefuse(f"plan_need_corrupt:missing_{field}")
    return parse_plan_need(prev[field])


def resolve_plan_needs(
    *,
    hypotheses: Need | None,
    literature: Need | None,
) -> tuple[Need, Need]:
    """Resolve Plan Needs for accept_plan. Omitted → PlanNeedRefuse (planner decides)."""
    if hypotheses is None:
        raise PlanNeedRefuse("plan_need_required:hypotheses")
    if literature is None:
        raise PlanNeedRefuse("plan_need_required:literature")
    return validate_need(hypotheses), validate_need(literature)
