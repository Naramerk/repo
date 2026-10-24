"""Plan Need helpers (G4). ``Need`` / ``NeedUnrecorded`` are in domain."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, get_args

from pydantic import Field, TypeAdapter, ValidationError

from coscientist.core.execution.domain import Need, NeedUnrecorded

NeedKind = Literal["hypotheses", "literature"]
PlanNeedField = Annotated[
    Need | NeedUnrecorded,
    Field(discriminator="kind"),
]
_PLAN_NEED_ADAPTER: TypeAdapter[Need | NeedUnrecorded] = TypeAdapter(PlanNeedField)

# Discriminator tags derived from Need / NeedUnrecorded kind Literals.
PLAN_NEED_KINDS: frozenset[str] = frozenset(
    {
        *get_args(Need.model_fields["kind"].annotation),
        *get_args(NeedUnrecorded.model_fields["kind"].annotation),
    }
)

PlanNeedRefuseReason = Literal[
    "plan_need_unrecorded",
    "invalid",
    "plan_need_required",
]


class PlanNeedRefuse(ValueError):
    """Typed refuse for Need / profile contradictions (not a silent fix)."""

    def __init__(self, detail: str, *, reason: PlanNeedRefuseReason) -> None:
        self.detail = detail
        self.reason = reason
        super().__init__(detail)


def parse_plan_need(raw: Mapping[str, Any]) -> Need | NeedUnrecorded:
    """Adapter: plan-blob / event dict → discriminated ``Need`` | ``NeedUnrecorded``."""
    return _PLAN_NEED_ADAPTER.validate_python(raw)


def require_need(value: Need | NeedUnrecorded) -> Need:
    """Fail-closed at use: ``NeedUnrecorded`` is not a planner decision."""
    if isinstance(value, NeedUnrecorded):
        raise PlanNeedRefuse(
            f"plan_need_unrecorded:source_schema_version={value.source_schema_version}",
            reason="plan_need_unrecorded",
        )
    return value


def need_from_plan_blob(prev: Mapping[str, Any] | None, field: NeedKind) -> Need | NeedUnrecorded:
    """Carry Need from active plan blob. Missing after upcast = corruption → refuse."""
    if prev is None or field not in prev or prev[field] is None:
        raise PlanNeedRefuse(f"plan_need_corrupt:missing_{field}", reason="invalid")
    raw = prev[field]
    if not isinstance(raw, Mapping):
        raise PlanNeedRefuse(f"plan_need_corrupt:bad_shape_{field}", reason="invalid")
    try:
        return parse_plan_need(raw)
    except ValidationError as e:
        raise PlanNeedRefuse(f"plan_need_corrupt:bad_shape_{field}", reason="invalid") from e


def recorded_plan_needs(prev: Mapping[str, Any] | None) -> tuple[Need, Need]:
    """Prod Plan builders: both fields must be recorded ``Need`` (fail-closed)."""
    return (
        require_need(need_from_plan_blob(prev, "hypotheses")),
        require_need(need_from_plan_blob(prev, "literature")),
    )


def resolve_plan_needs(
    *,
    hypotheses: Need | None,
    literature: Need | None,
) -> tuple[Need, Need]:
    """Resolve Plan Needs for accept_plan. Omitted → PlanNeedRefuse (planner decides)."""
    if hypotheses is None:
        raise PlanNeedRefuse("plan_need_required:hypotheses", reason="plan_need_required")
    if literature is None:
        raise PlanNeedRefuse("plan_need_required:literature", reason="plan_need_required")
    return hypotheses, literature
