"""Pure compute_outcome (DESIGN §4.2)."""

from __future__ import annotations

from collections.abc import Sequence

from coscientist.core.events.errors import CatalogError
from coscientist.core.execution.domain import (
    OUTCOME_RANK,
    CheckResult,
    Downgrade,
    NodeRequirement,
    Outcome,
    Provenance,
    ReqStatus,
)

OK = {ReqStatus.MET, ReqStatus.WAIVED, ReqStatus.NOT_APPLICABLE}


def compute_outcome(
    reqs: Sequence[NodeRequirement | CheckResult],
    claim: Outcome | None,
    reason: str | None,
    provenance: Provenance = "real",
) -> tuple[Outcome | None, Downgrade | None]:
    """None = still waiting on CodeCheck/ReviewCheck; task stays RUNNING."""
    req = [r for r in reqs if r.required]
    if not req:
        raise CatalogError("no required requirements")

    def status(r: NodeRequirement | CheckResult) -> ReqStatus:
        return r.status

    def is_code(r: NodeRequirement | CheckResult) -> bool:
        return r.is_code

    if any(status(r) == ReqStatus.PENDING for r in req):
        return None, None

    code = [r for r in req if is_code(r)]
    review = [r for r in req if not is_code(r)]

    if any(status(r) in {ReqStatus.FAILED, ReqStatus.MIGRATION_ERROR} for r in req):
        base = (
            Outcome.PARTIAL
            if any(status(r) in OK for r in req)
            else Outcome.FAILED
        )
    elif (
        code
        and all(status(r) in OK for r in code)
        and all(status(r) in OK | {ReqStatus.UNASSESSED} for r in review)
    ):
        # B2b: UNASSESSED review does not downgrade when all CodeChecks MET
        base = Outcome.SUCCESS
    elif all(status(r) in OK for r in req):
        base = Outcome.SUCCESS
    else:
        # only ReviewCheck and all UNASSESSED → PARTIAL
        base = Outcome.PARTIAL

    if provenance != "real":
        if OUTCOME_RANK[base] > OUTCOME_RANK[Outcome.PARTIAL]:
            base = Outcome.PARTIAL

    if claim is not None and OUTCOME_RANK[claim] < OUTCOME_RANK[base] and reason:
        return claim, Downgrade(from_=base, to=claim, reason=reason)
    return base, None
