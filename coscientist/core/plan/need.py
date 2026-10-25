"""Plan Need helpers (G4 / RC4). ``Need`` / ``NeedUnrecorded`` are in domain."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, get_args

from pydantic import Field, TypeAdapter, ValidationError

from coscientist.core.execution.domain import Need, NeedUnrecorded, TaskSpec

NeedKind = Literal["hypotheses", "literature"]
PlanNeedField = Annotated[
    Need | NeedUnrecorded,
    Field(discriminator="kind"),
]
_PLAN_NEED_ADAPTER: TypeAdapter[Need | NeedUnrecorded] = TypeAdapter(PlanNeedField)

# Canonical HYP-n keys (DESIGN §2). Kept here so upcasters→need stay free of
# expand_validate → machine edges (agent import contracts).
_HYP_N_KEY = re.compile(r"^HYP-(\d+)$")

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
    "profile_requires_hypotheses",
    "hyp_n_required",
]


class PlanNeedRefuse(ValueError):
    """Typed refuse for Need / profile contradictions (not a silent fix)."""

    def __init__(self, detail: str, *, reason: PlanNeedRefuseReason) -> None:
        self.detail = detail
        self.reason = reason
        super().__init__(detail)


def is_hyp_n_task_key(task_key: str) -> bool:
    """True iff ``task_key`` is canonical ``HYP-<n>`` (DESIGN §2 / expand keys)."""
    return _HYP_N_KEY.match(task_key) is not None


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


def assert_profile_hypotheses_satisfied(
    hypotheses: Need | NeedUnrecorded,
    *,
    require_hypotheses: bool,
) -> None:
    """Refuse when profile requires hypotheses and plan Need is off / unrecorded."""
    hyp = require_need(hypotheses)
    if require_hypotheses and not hyp.needed:
        raise PlanNeedRefuse(
            "profile_requires_hypotheses:plan.hypotheses.needed_must_be_true",
            reason="profile_requires_hypotheses",
        )


def assert_hyp_n_when_needed(tasks: Sequence[TaskSpec], hypotheses: Need) -> None:
    """RC4: when ``hypotheses.needed``, plan must include ≥1 ``HYP-n`` task.

    ``Study.require_hypotheses`` forces Need on via
    :func:`assert_profile_hypotheses_satisfied`; when the flag is false, Need alone
    decides (CAP #2 / plan RC4).
    """
    if not hypotheses.needed:
        return
    if any(is_hyp_n_task_key(t.task_key) for t in tasks):
        return
    raise PlanNeedRefuse(
        "hyp_n_required:plan_must_include_HYP-n",
        reason="hyp_n_required",
    )


def resolve_plan_needs(
    *,
    hypotheses: Need | None,
    literature: Need | None,
    require_hypotheses: bool,
) -> tuple[Need, Need]:
    """Resolve Plan Needs for accept_plan. Omitted → PlanNeedRefuse (planner decides)."""
    if hypotheses is None:
        raise PlanNeedRefuse("plan_need_required:hypotheses", reason="plan_need_required")
    if literature is None:
        raise PlanNeedRefuse("plan_need_required:literature", reason="plan_need_required")
    assert_profile_hypotheses_satisfied(hypotheses, require_hypotheses=require_hypotheses)
    return hypotheses, literature
