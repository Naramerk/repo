"""Plan Need helpers (G4). ``Need`` is in domain; resolve/validate live here."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from coscientist.core.execution.domain import Need

NeedKind = Literal["hypotheses", "literature"]

# Upcaster-only reason when PlanAccepted predates Need fields.
LEGACY_ABSENT_REASON = "legacy_absent"


class PlanNeedRefuse(ValueError):
    """Typed refuse for Need / profile contradictions (not a silent fix)."""


def validate_need(need: Need | dict[str, Any]) -> Need:
    """Parse/validate a Need; raise on empty reason or bad shape."""
    if isinstance(need, Need):
        return Need.model_validate(need.model_dump())
    return Need.model_validate(need)


def legacy_absent_need() -> Need:
    """Need for PlanAccepted upcast when hypotheses/literature were absent."""
    return Need(needed=False, reason=LEGACY_ABSENT_REASON)


def need_from_plan_blob(
    prev: Mapping[str, Any] | None,
    field: NeedKind,
) -> Need:
    """Carry Need from the active plan blob; absent → legacy_absent_need."""
    if prev is None:
        return legacy_absent_need()
    raw = prev.get(field)
    if raw is None:
        return legacy_absent_need()
    return validate_need(raw)


def profile_default_need(*, kind: NeedKind, require_hypotheses: bool) -> Need:
    """Default Need when accept_plan omits the field — from frozen Study flag only.

    ``literature`` is never forced by ``require_hypotheses`` (hypotheses-only).
    """
    if kind == "hypotheses" and require_hypotheses:
        return Need(needed=True, reason="profile_requires_hypotheses")
    if kind == "hypotheses":
        return Need(needed=False, reason="profile_optional_hypotheses")
    return Need(needed=False, reason="profile_optional_literature")


def assert_profile_hypotheses_satisfied(
    hypotheses: Need,
    *,
    require_hypotheses: bool,
) -> None:
    """Refuse explicit ``needed=False`` when the study profile requires hypotheses."""
    if require_hypotheses and not hypotheses.needed:
        raise PlanNeedRefuse(
            "profile_requires_hypotheses:plan.hypotheses.needed_must_be_true"
        )


def resolve_plan_needs(
    *,
    hypotheses: Need | dict[str, Any] | None,
    literature: Need | dict[str, Any] | None,
    require_hypotheses: bool,
) -> tuple[Need, Need]:
    """Resolve Plan.hypotheses / Plan.literature for accept_plan.

    Explicit values are validated. Omitted values come from
    ``profile_default_need`` (Study.require_hypotheses), never from key shape.
    """
    if hypotheses is None:
        hyp = profile_default_need(
            kind="hypotheses", require_hypotheses=require_hypotheses
        )
    else:
        hyp = validate_need(hypotheses)
    if literature is None:
        lit = profile_default_need(
            kind="literature", require_hypotheses=require_hypotheses
        )
    else:
        lit = validate_need(literature)
    assert_profile_hypotheses_satisfied(hyp, require_hypotheses=require_hypotheses)
    return hyp, lit
