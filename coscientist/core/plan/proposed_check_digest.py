"""Proposed-check prereg digest + content validation (M1 PR20).

NFKC digests and fail-closed ``validate_proposal_content`` live here so
``proposed_check`` can own propose / approve / review flows without a
god-module. Callers import from this module directly (no re-export).
"""

from __future__ import annotations

import math
import unicodedata
from typing import Any, Literal, Mapping

from coscientist.core.claims.rubric import freeze_rubric
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.execution.domain import (
    PROPOSED_CHECK_TEMPLATE_ID,
    CheckProposal,
)

ProposalStatus = Literal["proposed", "approved", "rejected"]
ApprovalSource = Literal["hitl", "api", "reviewer"]

# Executable kinds allowed inside a proposed CodeCheck (PR17 + simple predicates).
_ALLOWED_PROPOSED_KINDS = frozenset(
    {
        "artifact_exists",
        "schema_valid",
        "json_pointer",
        "source_link",
        "metric_threshold",
        "value_matches",
        "exec_runs",
        "tests_pass",
        "metric_on_holdout",
        "reproducible_rerun",
    }
)


_PREREG_FIELDS = frozenset(
    {
        "target",
        "target_kind",
        "role",
        "measures",
        "check",
        "rubric",
        "threshold",
        "sesoi",
        "uses_sesoi",
        "predicts_null",
        "predicted_direction",
        "justification",
        "rejected_templates",
    }
)


def _nfkc(value: Any) -> Any:
    """NFKC-normalize strings (and nested string leaves) for stable digests.

    Limitation: NFKC does not collapse cross-script confusables (Cyrillic/Latin
    lookalikes, etc.); only Unicode compatibility equivalents (e.g. fullwidth).
    """
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value)
    if isinstance(value, list):
        return [_nfkc(v) for v in value]
    if isinstance(value, dict):
        return {str(_nfkc(k)): _nfkc(v) for k, v in value.items()}
    return value


def proposal_prereg_body(proposal: CheckProposal | Mapping[str, Any]) -> dict[str, Any]:
    """Canonical body hashed at CheckProposed (excludes approval / runtime ids)."""
    if isinstance(proposal, CheckProposal):
        raw = proposal.model_dump(mode="json")
    else:
        raw = dict(proposal)
    # Whitelist only scientific preregistration fields (ignore status/delegated/…).
    body = {k: raw[k] for k in sorted(_PREREG_FIELDS) if k in raw}
    return _nfkc(body)  # type: ignore[return-value]


def compute_proposal_digest(proposal: CheckProposal | Mapping[str, Any]) -> str:
    """sha256 of JCS(NFKC(prereg body)) — fixed before any check result."""
    return sha256_hex(canonical_json(proposal_prereg_body(proposal)))


def _reject_non_finite_threshold(proposal: CheckProposal) -> None:
    for name in ("threshold", "sesoi"):
        val = getattr(proposal, name, None)
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            raise ValueError(f"CheckProposal.{name} must be finite (not NaN/inf)")
        if isinstance(val, dict):
            for v in val.values():
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    raise ValueError(f"CheckProposal.{name} must be finite (not NaN/inf)")


def validate_proposal_content(proposal: CheckProposal) -> None:
    """Fail-closed validation before journal write."""
    if not proposal.target or not str(proposal.target).strip():
        raise ValueError("CheckProposal.target required")
    if not proposal.measures or not str(proposal.measures).strip():
        raise ValueError("CheckProposal.measures required")
    if not proposal.justification or not str(proposal.justification).strip():
        raise ValueError("CheckProposal.justification required")
    if not proposal.rejected_templates:
        raise ValueError(
            "CheckProposal.rejected_templates required "
            "(justify why nearest catalog templates do not fit)"
        )
    for note in proposal.rejected_templates:
        if not note.template_id or not note.reason:
            raise ValueError("rejected_templates entries need template_id and reason")
        if note.template_id == PROPOSED_CHECK_TEMPLATE_ID:
            raise ValueError("cannot reject the proposed_check meta-template itself")
    has_check = proposal.check is not None
    has_rubric = proposal.rubric is not None
    if has_check == has_rubric:
        raise ValueError("CheckProposal needs exactly one of check= or rubric=")
    if proposal.check is not None:
        kind = proposal.check.kind
        if kind not in _ALLOWED_PROPOSED_KINDS:
            raise ValueError(f"proposed CodeCheck kind not allowed: {kind!r}")
        if kind == "rubric":
            raise ValueError("use rubric= for qualitative proposals, not check.kind=rubric")
    if proposal.rubric is not None:
        freeze_rubric(proposal.rubric)  # validates aggregate / ids
    if proposal.role not in ("prediction", "validity"):
        raise ValueError(f"bad CheckProposal.role: {proposal.role!r}")
    if proposal.uses_sesoi and proposal.sesoi is None and proposal.threshold is None:
        raise ValueError("uses_sesoi requires sesoi or threshold")
    _reject_non_finite_threshold(proposal)
