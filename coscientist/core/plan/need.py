"""Plan Need helpers (G4 / RC4 / RC5 / RC6 / RC7r). ``Need`` / ``NeedUnrecorded`` are in domain."""

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
    "hypotheses_lt_three",
    "hypotheses_no_selected",
    "hypotheses_selected_not_unique",
    "selected_hyp_ref_required",
    "hitl_reject",
]

# CAP #2 / RC7r: HITL reject records this status (no invent of other values here).
_HITL_REJECTED: Literal["rejected"] = "rejected"


class PlanNeedRefuse(ValueError):
    """Typed refuse for Need / profile / hypotheses-artifact contradictions."""

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



def _hyp_row_id(row: Mapping[str, Any], *, index: int) -> str:
    """Logical hyp id from artifact row (``id`` / ``hypothesis_id``); no invent."""
    for key in ("id", "hypothesis_id"):
        if key not in row:
            continue
        raw = row[key]
        if type(raw) is not str or not raw.strip():
            raise PlanNeedRefuse(
                f"hypotheses_artifact_invalid:bad_{key}:index={index}",
                reason="invalid",
            )
        return raw
    raise PlanNeedRefuse(
        f"hypotheses_artifact_invalid:missing_id:index={index}",
        reason="invalid",
    )


def _require_nonblank_str(row: Mapping[str, Any], field: str, *, index: int) -> str:
    if field not in row:
        raise PlanNeedRefuse(
            f"hypotheses_artifact_invalid:missing_{field}:index={index}",
            reason="invalid",
        )
    raw = row[field]
    if type(raw) is not str or not raw.strip():
        raise PlanNeedRefuse(
            f"hypotheses_artifact_invalid:bad_{field}:index={index}",
            reason="invalid",
        )
    return raw


def _require_rank(row: Mapping[str, Any], *, index: int) -> int:
    if "rank" not in row:
        raise PlanNeedRefuse(
            f"hypotheses_artifact_invalid:missing_rank:index={index}",
            reason="invalid",
        )
    raw = row["rank"]
    if type(raw) is bool or type(raw) is not int:
        raise PlanNeedRefuse(
            f"hypotheses_artifact_invalid:bad_rank:index={index}",
            reason="invalid",
        )
    return raw


def validate_hypotheses_artifact(hyps: Sequence[Mapping[str, Any]]) -> None:
    """RC5 / CAP #2: ≥3 hypotheses; exactly one ``selected=True`` (strict fields)."""
    # CAP #2 floor is three (literal inline — not a construct default).
    if len(hyps) < 3:
        raise PlanNeedRefuse(
            f"hypotheses_lt_three:count={len(hyps)}",
            reason="hypotheses_lt_three",
        )
    selected_n = 0
    seen_ids: set[str] = set()
    for i, row in enumerate(hyps):
        if not isinstance(row, Mapping):
            raise PlanNeedRefuse(
                f"hypotheses_artifact_invalid:bad_row:index={i}",
                reason="invalid",
            )
        hid = _hyp_row_id(row, index=i)
        if hid in seen_ids:
            raise PlanNeedRefuse(
                f"hypotheses_artifact_invalid:duplicate_id:{hid}",
                reason="invalid",
            )
        seen_ids.add(hid)
        _require_nonblank_str(row, "statement", index=i)
        _require_nonblank_str(row, "prediction", index=i)
        _require_rank(row, index=i)
        if "selected" not in row:
            raise PlanNeedRefuse(
                f"hypotheses_artifact_invalid:missing_selected:index={i}",
                reason="invalid",
            )
        selected = row["selected"]
        if type(selected) is not bool:
            raise PlanNeedRefuse(
                f"hypotheses_artifact_invalid:bad_selected:index={i}",
                reason="invalid",
            )
        if selected is True:
            selected_n += 1
    if selected_n == 0:
        raise PlanNeedRefuse(
            "hypotheses_no_selected:exactly_one_required",
            reason="hypotheses_no_selected",
        )
    if selected_n != 1:
        raise PlanNeedRefuse(
            f"hypotheses_selected_not_unique:count={selected_n}",
            reason="hypotheses_selected_not_unique",
        )


def selected_hypothesis_id(hyps: Sequence[Mapping[str, Any]]) -> str:
    """RC5-validate artifact then return the sole ``selected=True`` hyp id."""
    validate_hypotheses_artifact(hyps)
    for i, row in enumerate(hyps):
        if row["selected"] is True:
            return _hyp_row_id(row, index=i)
    raise PlanNeedRefuse(
        "hypotheses_no_selected:exactly_one_required",
        reason="hypotheses_no_selected",
    )


def assert_plan_refs_selected_hyp(
    tasks: Sequence[TaskSpec],
    hyps: Sequence[Mapping[str, Any]],
) -> None:
    """RC6 / CAP #2: ≥1 plan task must reference the selected hypothesis id.

    Reference = ``TaskSpec.foreach_item`` equal to the selected id (bound after
    foreach materialization over the HYP-n artifact).
    """
    selected = selected_hypothesis_id(hyps)
    for task in tasks:
        if task.foreach_item == selected:
            return
    raise PlanNeedRefuse(
        f"selected_hyp_ref_required:selected_id={selected}",
        reason="selected_hyp_ref_required",
    )


def assert_foreach_items_include_selected(
    items: Sequence[str],
    hyps: Sequence[Mapping[str, Any]],
) -> None:
    """RC6: foreach expand ``items`` must include the selected hyp id."""
    selected = selected_hypothesis_id(hyps)
    if selected in items:
        return
    raise PlanNeedRefuse(
        f"selected_hyp_ref_required:selected_id={selected}",
        reason="selected_hyp_ref_required",
    )


def hitl_reject_status(decision: str) -> Literal["rejected"]:
    """RC7r / CAP #2: HITL ``reject`` → status ``rejected`` (fail-closed; no invent)."""
    if type(decision) is not str:
        raise PlanNeedRefuse(
            "hitl_reject:decision_must_be_str",
            reason="invalid",
        )
    if decision != "reject":
        raise PlanNeedRefuse(
            f"hitl_reject:expected_reject:got={decision!r}",
            reason="invalid",
        )
    return _HITL_REJECTED


def _hitl_open_kind(opened: Any) -> str | None:
    """Kind from folded ``_hitl_requests`` row or live ``HitlRequest`` (no invent)."""
    if opened is None:
        return None
    if isinstance(opened, Mapping):
        kind = opened.get("kind")
        return kind if type(kind) is str else None
    kind = getattr(opened, "kind", None)
    return kind if type(kind) is str else None


def hypotheses_hitl_reject_status(
    *,
    hitl_opens: Mapping[str, Any] | None,
    hitl_decisions: Mapping[str, Any] | None,
) -> Literal["rejected"] | None:
    """Restore RC7r status from folded HITL maps (journal replay).

    ``rejected`` only when a ``HitlOpened(kind=hypotheses)`` has a matching
    ``HitlDecided(decision=reject)``. Opens may be core ``_hitl_requests``
    dicts or experimental live ``hitl`` ``HitlRequest`` values. Otherwise
    ``None`` (do not invent).
    """
    if not isinstance(hitl_opens, Mapping) or not isinstance(hitl_decisions, Mapping):
        return None
    for rid, dec in hitl_decisions.items():
        if not isinstance(dec, Mapping):
            continue
        if dec.get("decision") != "reject":
            continue
        if _hitl_open_kind(hitl_opens.get(rid)) == "hypotheses":
            return _HITL_REJECTED
    return None


def assert_hitl_reject_blocks_plan_accepted(
    *,
    decision: str | None = None,
    status: str | None = None,
) -> None:
    """RC7r core rule: HITL reject / status ``rejected`` ⇒ no ``PlanAccepted``."""
    if decision == "reject" or status == _HITL_REJECTED:
        raise PlanNeedRefuse(
            "hitl_reject:no_plan_accepted",
            reason="hitl_reject",
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
