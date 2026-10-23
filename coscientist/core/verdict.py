"""Hypothesis verdict model (M1 PR8b / DESIGN_HYP_DELTA).

Pure functions: claim classification helpers, counting filter, hypothesis
``verdict()``, optional e-value aggregation, and report payload builders.

Deviations from the written SPEC (see PR body / CHANGELOG):
- ``VerdictSnapshotsRecorded`` kept (not projection-only).
- StudyCreated is v5 (SPEC said v4; confirmations_needed already took v4).
- validity_failed / unknown_role per-experiment exclusion from PR8 kept.
- Real journal events use GraphNodeUpserted (claims), not ClaimRecorded.
- When criterion has neither sesoi nor min_power, severe uses a transitional
  gate (frozen + consistent + valid) so M1 catalogs without SESOI still work;
  R17 runtime metadata remains deferred (PR8).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from coscientist.core.digest import canonical_json
from coscientist.core.rules import DEFAULT_TESTS_INTENT, TestsIntent, claim_stance

ClaimOutcome = Literal["consistent", "inconsistent", "indeterminate"]
ClaimValidity = Literal["valid", "invalid"]
ExclusionReason = Literal[
    "stub",
    "partial",
    "invalid",
    "indeterminate",
    "lit",
    "r17",
    "superseded",
    "unknown_role",
    "validity_failed",
]


class _HypClaim(Protocol):
    accepted: bool
    provenance: str
    intent: TestsIntent
    criterion_status: str
    evidence_kind: str
    frozen: bool
    attempt_id: str
    artifact_id: str
    validity_ok: bool
    check_role: str

    @property
    def is_lit(self) -> bool: ...

    def counts(self) -> bool: ...


def status_to_outcome(criterion_status: str) -> ClaimOutcome | None:
    """Backward-compat mapping (SPEC §1 removed semantics)."""
    if criterion_status == "met":
        return "consistent"
    if criterion_status == "not_met":
        return "inconsistent"
    if criterion_status == "unassessed":
        return "indeterminate"
    return None


def outcome_to_status(outcome: ClaimOutcome | None, *, validity: ClaimValidity) -> str:
    if validity == "invalid" or outcome is None:
        return "unassessed"
    if outcome == "consistent":
        return "met"
    if outcome == "inconsistent":
        return "not_met"
    return "unassessed"


def independence_key_for(
    *,
    dataset_digest: str | None = None,
    method: str | None = None,
    code_digest: str | None = None,
    experiment_id: str | None = None,
) -> str:
    """sha256 of JCS({dataset_digest, method, code_digest}); fallback experiment_id."""
    payload = {
        "dataset_digest": dataset_digest or "",
        "method": method or "",
        "code_digest": code_digest or "",
    }
    if not any(payload.values()) and experiment_id:
        payload = {"experiment_id": experiment_id}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def effective_outcome(claim: Any) -> ClaimOutcome | None:
    """Read stored outcome, else derive from criterion_status / validity."""
    stored = getattr(claim, "claim_outcome", None) or getattr(claim, "outcome", None)
    if stored in ("consistent", "inconsistent", "indeterminate"):
        return stored  # type: ignore[return-value]
    if getattr(claim, "validity_ok", True) is False:
        return None
    return status_to_outcome(str(getattr(claim, "criterion_status", "unassessed")))


def effective_validity(claim: Any) -> ClaimValidity:
    if getattr(claim, "validity_ok", True) is False:
        return "invalid"
    stored = getattr(claim, "validity", None)
    if stored in ("valid", "invalid"):
        return stored  # type: ignore[return-value]
    return "valid"


def claim_is_severe(claim: Any) -> bool:
    """SPEC §3.1 severe gate (+ M1 transitional when no SESOI/power on criterion)."""
    if effective_outcome(claim) != "consistent":
        return False
    if effective_validity(claim) != "valid":
        return False
    if getattr(claim, "severe", None) is True:
        return True
    if getattr(claim, "severe", None) is False and (
        getattr(claim, "claim_outcome", None) is not None
        or getattr(claim, "outcome", None) is not None
    ):
        # Explicitly classified non-severe.
        return False
    sesoi = getattr(claim, "sesoi", None)
    min_power = getattr(claim, "min_power", None)
    power = getattr(claim, "power", None)
    frozen_at = getattr(claim, "criterion_frozen_at_seq", None)
    first_ev = getattr(claim, "first_cited_evidence_seq", None)
    # R17: when metadata present, require freeze-before-evidence.
    if frozen_at is not None and first_ev is not None and not (frozen_at < first_ev):
        return False
    if min_power is not None and (power if power is not None else 0.0) < float(min_power):
        return False
    if sesoi is None and min_power is None:
        # Transitional: catalogs without SESOI/power still allow severe when frozen.
        return bool(getattr(claim, "frozen", False))
    return True


def exclusion_reason(claim: Any) -> ExclusionReason | None:
    """Why a claim is out of the counting set (SPEC §3.2 + PR8 roles)."""
    if claim.counts():
        return None
    stored = getattr(claim, "exclusion_reason", None)
    if stored in (
        "stub",
        "partial",
        "invalid",
        "indeterminate",
        "lit",
        "r17",
        "superseded",
        "unknown_role",
        "validity_failed",
    ):
        return stored  # type: ignore[return-value]
    if getattr(claim, "check_role", "unknown") == "unknown" and not getattr(
        claim, "validity_ok", True
    ):
        return "unknown_role"
    if effective_validity(claim) == "invalid":
        return "validity_failed"
    prov = getattr(claim, "provenance", "stub")
    if prov == "stub":
        return "stub"
    if prov == "partial":
        return "partial"
    if getattr(claim, "is_lit", False) or getattr(claim, "evidence_kind", "") == "literature":
        return "lit"
    oc = effective_outcome(claim)
    if oc == "indeterminate" or oc is None:
        return "indeterminate"
    return "invalid"


def distinct_by_key(claims: Sequence[Any]) -> list[Any]:
    """Keep first claim per independence_key (or attempt_id fallback)."""
    seen: set[str] = set()
    out: list[Any] = []
    for c in claims:
        key = (
            getattr(c, "independence_key", None)
            or independence_key_for(experiment_id=getattr(c, "attempt_id", "") or "x")
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _intent(claim: Any) -> TestsIntent:
    intent = getattr(claim, "intent", None) or DEFAULT_TESTS_INTENT
    if intent not in ("confirm", "refute"):
        return DEFAULT_TESTS_INTENT
    return intent  # type: ignore[return-value]


def _calibrate_e(p: float, kappa: float = 0.5) -> float:
    if p <= 0:
        return 1.0 / kappa  # large
    return kappa * (p ** (kappa - 1.0))


def evalue_verdict(
    claims: Sequence[Any],
    *,
    evalue_alpha: float = 0.05,
) -> str:
    """POPPER-style product of calibrated p-values (SPEC §3.4)."""
    by_key_support: dict[str, float] = {}
    by_key_refute: dict[str, float] = {}
    any_severe = False
    for c in claims:
        if not c.counts():
            continue
        p = getattr(c, "p_value", None)
        key = str(
            getattr(c, "independence_key", None) or getattr(c, "attempt_id", None) or "x"
        )
        oc = effective_outcome(c)
        if claim_is_severe(c):
            any_severe = True
        if p is None:
            continue
        e = _calibrate_e(float(p))
        if oc == "consistent":
            by_key_support[key] = max(by_key_support.get(key, 0.0), e)
        elif oc == "inconsistent":
            by_key_refute[key] = max(by_key_refute.get(key, 0.0), e)
    e_sup = 1.0
    for v in by_key_support.values():
        e_sup *= v
    e_ref = 1.0
    for v in by_key_refute.values():
        e_ref *= v
    thr = 1.0 / float(evalue_alpha)
    if e_ref >= thr:
        return "refuted"
    if e_sup >= thr and len(by_key_support) >= 2:
        return "supported"
    if any_severe:
        return "survived"
    return "inconclusive"


def hypothesis_verdict(
    claims: Sequence[Any],
    any_experiment_done: bool = False,
    *,
    refutations_needed: int = 1,
    refutation_priority: bool = True,
    min_independent_support: int = 2,
    aggregate: Literal["count", "evalue"] = "count",
    evalue_alpha: float = 0.05,
    confirmations_needed: int = 1,  # retained for API compat; unused in count path
) -> tuple[str, bool, str | None]:
    """Hypothesis/question verdict (SPEC §3.3).

    Returns ``(verdict, by_literature, rule)``. ``by_literature`` is always
    False under the new filter (lit never counts). ``confirmations_needed`` is
    accepted for call-site compat but support uses ``min_independent_support``.
    """
    del confirmations_needed
    about = list(claims)
    if not about and not any_experiment_done:
        return "untested", False, "no_counted"
    if not about:
        return "untested", False, "no_counted"

    # Prefer R17-aware counts when available (refmodel.counts_under_r17 / rules).
    def _counts(c: Any) -> bool:
        fn = getattr(c, "counts_under_r17", None)
        if callable(fn):
            return bool(fn())
        from coscientist.core.rules import _claim_counts

        return bool(_claim_counts(c))

    counted = [c for c in about if _counts(c)]
    if aggregate == "evalue":
        v = evalue_verdict(counted, evalue_alpha=evalue_alpha)
        return v, False, "evalue"

    inconsistent = distinct_by_key(
        [c for c in counted if effective_outcome(c) == "inconsistent"]
    )
    consistent_severe = distinct_by_key(
        [
            c
            for c in counted
            if effective_outcome(c) == "consistent" and claim_is_severe(c)
        ]
    )
    k_ref = max(1, int(refutations_needed))
    n_inc = len(inconsistent)
    n_con = len(consistent_severe)

    if n_inc >= k_ref and refutation_priority:
        return "refuted", False, "k_ref"
    if n_inc >= k_ref and not refutation_priority:
        if consistent_severe:
            return "inconclusive", False, "k_ref"
        return "refuted", False, "k_ref"
    if n_inc > 0:
        return "inconclusive", False, "inc_below_k_ref"
    if n_con >= max(1, int(min_independent_support)) and any(
        _intent(c) == "refute" for c in consistent_severe
    ):
        return "supported", False, "support_min"
    if n_con >= 1:
        return "survived", False, "single_severe"
    if counted:
        return "inconclusive", False, "no_counted"
    return "inconclusive", False, "no_counted"


def verdict(
    claims: Sequence[Any],
    any_experiment_done: bool,
    k: int = 1,
    *,
    refutations_needed: int = 1,
    refutation_priority: bool = False,
    min_independent_support: int = 2,
    aggregate: Literal["count", "evalue"] = "count",
    evalue_alpha: float = 0.05,
) -> tuple[str, bool]:
    """Compat wrapper matching ``rules.verdict`` return shape."""
    v, by_lit, _rule = hypothesis_verdict(
        claims,
        any_experiment_done,
        refutations_needed=refutations_needed,
        refutation_priority=refutation_priority,
        min_independent_support=min_independent_support,
        aggregate=aggregate,
        evalue_alpha=evalue_alpha,
        confirmations_needed=k,
    )
    return v, by_lit


def excluded_confirmations_note(
    claims: Sequence[Any],
    *,
    verdict_value: str,
) -> str | None:
    """Report note when refuted and stub/partial/etc. confirmations were excluded."""
    if verdict_value != "refuted":
        return None
    excluded = [c for c in claims if not c.counts()]
    stub_partial = []
    for c in excluded:
        if getattr(c, "provenance", "") not in ("stub", "partial"):
            continue
        oc = effective_outcome(c)
        stance = claim_stance(str(getattr(c, "criterion_status", "unassessed")))
        if oc == "consistent" or stance == "supports":
            stub_partial.append(c)
    # Prefer distinct attempt ids among excluded supporting stubs/partials.
    keys = {
        getattr(c, "attempt_id", "") or getattr(c, "artifact_id", "") or id(c)
        for c in stub_partial
    }
    keys.discard("")
    n = len(keys) if keys else len(stub_partial)
    if n <= 0:
        # Generic excluded count when no stub supports but other exclusions exist.
        reasons: dict[str, int] = {}
        for c in excluded:
            r = exclusion_reason(c) or "invalid"
            reasons[r] = reasons.get(r, 0) + 1
        if not reasons:
            return None
        parts = ", ".join(f"{r} {m}" for r, m in sorted(reasons.items()))
        return f"refuted; {sum(reasons.values())} claims excluded ({parts})"
    return f"refuted; {n} supporting results were stub/partial"


def hypothesis_report_row(
    *,
    hypothesis_id: str,
    question_id: str,
    claims: Sequence[Any],
    verdict_value: str,
    rule: str | None,
    legacy_claims: int = 0,
) -> dict[str, Any]:
    """SPEC §5 hypotheses[] item."""
    counted_rows: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    supporting: list[str] = []
    refuting: list[str] = []
    for c in claims:
        cid = getattr(c, "node_id", None) or getattr(c, "attempt_id", "") or "?"
        if c.counts():
            oc = effective_outcome(c) or "indeterminate"
            counted_rows.append(
                {
                    "claim_id": cid,
                    "outcome": oc,
                    "intent": _intent(c),
                    "severe": claim_is_severe(c),
                    "independence_key": getattr(c, "independence_key", "") or "",
                }
            )
            if oc == "consistent":
                supporting.append(cid)
            elif oc == "inconsistent":
                refuting.append(cid)
        else:
            excluded_rows.append(
                {"claim_id": cid, "reason": exclusion_reason(c) or "invalid"}
            )
    conflict = None
    if verdict_value == "refuted" and supporting and refuting:
        conflict = {"supporting": supporting, "refuting": refuting}
    return {
        "hypothesis_id": hypothesis_id,
        "question_id": question_id,
        "verdict": verdict_value,
        "rule": rule or "no_counted",
        "counted": counted_rows,
        "excluded": excluded_rows,
        "conflict": conflict,
        "severity_note": (
            "survived ≠ proven; tests that could have refuted are listed in counted"
            if verdict_value in ("survived", "supported")
            else None
        ),
        "auxiliary_assumptions": [],
        "legacy_claims": legacy_claims,
        "e_values": None,
    }


def disposition_for_question(
    *,
    hypothesis_verdicts: Mapping[str, str],
    question_verdict: str | None,
    has_hypotheses: bool,
) -> str:
    """SPEC §3.5 disposition mapping."""
    if not has_hypotheses:
        mapping = {
            "supported": "answered",
            "survived": "survived",
            "refuted": "refuted",
            "inconclusive": "inconclusive",
            "untested": "not_attempted",
        }
        return mapping.get(question_verdict or "untested", "inconclusive")
    vals = list(hypothesis_verdicts.values())
    if any(v == "supported" for v in vals):
        return "answered"
    if any(v == "survived" for v in vals) and not any(v == "supported" for v in vals):
        return "survived"
    if vals and all(v == "refuted" for v in vals):
        return "refuted"
    return "inconclusive"
