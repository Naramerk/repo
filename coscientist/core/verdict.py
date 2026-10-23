"""Hypothesis verdict model (M1 PR8b / DESIGN_HYP_DELTA).

Pure functions: claim classification, counting filter, hypothesis ``verdict()``,
and report payload builders.

Deviations (see PR body / CHANGELOG):
- ``VerdictSnapshotsRecorded`` kept (not projection-only).
- StudyCreated is v6 (``supported_needed``; confirmations_needed remains PR8 ``k``).
- validity_failed / unknown_role per-experiment exclusion from PR8 kept.
- Real journal events use GraphNodeUpserted (claims), not ClaimRecorded.
- E-value aggregation deferred (``aggregate='evalue'`` rejected at StudyCreated).
- R17 runtime metadata still deferred (PR8).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from coscientist.core.digest import canonical_json
from coscientist.core.stance import DEFAULT_TESTS_INTENT, TestsIntent, claim_stance

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


def digests_from_bindings(
    bindings: Sequence[Any] | None,
) -> tuple[str | None, str | None]:
    """Fold InputBinding digests into (dataset_digest, code_digest).

    Artifact inputs whose name suggests code/script/program/model → code;
    other artifact bindings → dataset. Non-artifact bindings are ignored.
    """
    data: list[str] = []
    code: list[str] = []
    for b in bindings or ():
        if isinstance(b, Mapping):
            kind = b.get("kind", "artifact")
            name = str(b.get("input") or "")
            digest = b.get("digest")
        else:
            kind = getattr(b, "kind", "artifact")
            name = str(getattr(b, "input", "") or "")
            digest = getattr(b, "digest", None)
        if kind != "artifact" or not digest:
            continue
        d = str(digest)
        lower = name.lower()
        if any(tok in lower for tok in ("code", "script", "program", "model", "source")):
            code.append(d)
        else:
            data.append(d)

    def _fold(parts: list[str]) -> str | None:
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return hashlib.sha256(canonical_json(sorted(parts))).hexdigest()

    return _fold(data), _fold(code)


def independence_key_for(
    *,
    dataset_digest: str | None = None,
    method: str | None = None,
    code_digest: str | None = None,
    experiment_id: str | None = None,
) -> str:
    """sha256(JCS({dataset_digest, method, code_digest})); else experiment_id.

    ``method=None`` becomes ``""`` so omitting method cannot inflate independence.
    Fallback uses ``experiment_id`` (task id), never attempt id.
    """
    payload = {
        "dataset_digest": dataset_digest or "",
        "method": method or "",
        "code_digest": code_digest or "",
    }
    if not any(payload.values()):
        payload = {"experiment_id": experiment_id or ""}
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


def tost_holds(*, sesoi: float | None, effect: Mapping[str, Any] | None) -> bool:
    """Equivalence / null claim: CI strictly inside (−sesoi, +sesoi)."""
    if sesoi is None or effect is None:
        return False
    try:
        lo = float(effect["ci_low"])  # type: ignore[index]
        hi = float(effect["ci_high"])  # type: ignore[index]
        s = float(sesoi)
    except (KeyError, TypeError, ValueError):
        return False
    return (-s) < lo and hi < s


def _threshold_set(*, sesoi: float | None, min_power: float | None) -> bool:
    """sesoi set, or min_power > 0 (zero / absent does not qualify)."""
    if sesoi is not None:
        return True
    if min_power is not None and float(min_power) > 0.0:
        return True
    return False


def claim_is_severe(claim: Any) -> bool:
    """Severe: consistent ∧ valid ∧ frozen ∧ (sesoi|min_power>0) ∧ power gate."""
    if effective_outcome(claim) != "consistent":
        return False
    if effective_validity(claim) != "valid":
        return False
    # Legacy / explicit non-severe stamp is definitive (V5).
    if getattr(claim, "severe", None) is False:
        return False
    if not bool(getattr(claim, "frozen", False)):
        return False
    sesoi = getattr(claim, "sesoi", None)
    min_power = getattr(claim, "min_power", None)
    power = getattr(claim, "power", None)
    frozen_at = getattr(claim, "criterion_frozen_at_seq", None)
    first_ev = getattr(claim, "first_cited_evidence_seq", None)
    if frozen_at is not None and first_ev is not None and not (frozen_at < first_ev):
        return False
    if not _threshold_set(sesoi=sesoi, min_power=min_power):
        return False
    if min_power is not None and float(min_power) > 0.0:
        if (power if power is not None else 0.0) < float(min_power):
            return False
    return True


def refute_intent_counts(claim: Any) -> bool:
    """Refute intent counts toward supported only if the prediction could fail.

    Requires: intent=refute, frozen criterion, sesoi|min_power>0, prediction role.
    A planner label alone is not enough.
    """
    if _intent(claim) != "refute":
        return False
    if getattr(claim, "refute_capable", None) is False:
        return False
    if getattr(claim, "refute_capable", None) is True:
        return True
    # Derive when stamp absent (unit tests / legacy).
    if not bool(getattr(claim, "frozen", False)):
        return False
    if getattr(claim, "check_role", "unknown") != "prediction":
        return False
    return _threshold_set(
        sesoi=getattr(claim, "sesoi", None),
        min_power=getattr(claim, "min_power", None),
    )


def classify_outcome(
    *,
    validity_ok: bool,
    prediction_statuses: Sequence[str],
    predicts_null: bool = False,
    sesoi: float | None = None,
    effect: Mapping[str, Any] | None = None,
    r17_ok: bool = True,
) -> tuple[str, str | None, bool]:
    """Return (validity, outcome|None, severe_hint_inputs_ok)."""
    if not validity_ok or not r17_ok:
        return "invalid", None, False
    preds = [s for s in prediction_statuses if s]
    if any(s == "FAILED" or s == "not_met" for s in preds):
        outcome: str | None = "inconsistent"
    elif preds and all(s in ("PASSED", "MET", "met") for s in preds):
        outcome = "consistent"
    else:
        outcome = "indeterminate"
    if predicts_null and outcome == "consistent":
        if not tost_holds(sesoi=sesoi, effect=effect):
            outcome = "indeterminate"
    return "valid", outcome, outcome == "consistent"


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
    """Keep first claim per independence_key (experiment_id fallback)."""
    seen: set[str] = set()
    out: list[Any] = []
    for c in claims:
        key = getattr(c, "independence_key", None) or independence_key_for(
            experiment_id=getattr(c, "experiment_id", None)
            or getattr(c, "attempt_id", "")
            or "x"
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


def hypothesis_verdict(
    claims: Sequence[Any],
    any_experiment_done: bool = False,
    *,
    refutations_needed: int = 1,
    refutation_priority: bool = True,
    supported_needed: int = 2,
    confirmations_needed: int = 1,
) -> tuple[str, bool, str | None]:
    """Hypothesis/question verdict (SPEC §5).

    ``supported_needed`` (≥2) is the severe-support threshold. ``confirmations_needed``
    is retained for PR8 snapshot ``k`` compat and is not the support gate.
    Returns ``(verdict, by_literature, rule)``.
    """
    del confirmations_needed  # PR8 k — not used for supported gate
    del any_experiment_done
    about = list(claims)
    if not about:
        return "untested", False, "no_counted"

    def _counts(c: Any) -> bool:
        fn = getattr(c, "counts_under_r17", None)
        if callable(fn):
            return bool(fn())
        from coscientist.core.rules import _claim_counts

        return bool(_claim_counts(c))

    counted = [c for c in about if _counts(c)]

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
    k_sup = max(2, int(supported_needed))
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
    if n_con >= k_sup and any(refute_intent_counts(c) for c in consistent_severe):
        return "supported", False, "support_min"
    if n_con >= 1:
        return "survived", False, "single_severe"
    if counted:
        return "inconclusive", False, "no_counted"
    if any(effective_validity(c) == "invalid" for c in about):
        return "inconclusive", False, "validity_failed"
    # Only excluded (stub/partial/lit/…) claims — inconclusive, not untested.
    return "inconclusive", False, "no_counted"


def verdict(
    claims: Sequence[Any],
    any_experiment_done: bool,
    k: int = 1,
    *,
    refutations_needed: int = 1,
    refutation_priority: bool = False,
    supported_needed: int = 2,
) -> tuple[str, bool]:
    """Compat wrapper matching ``rules.verdict`` return shape.

    ``k`` is PR8 ``confirmations_needed`` (snapshot field); support gate uses
    ``supported_needed``.
    """
    v, by_lit, _rule = hypothesis_verdict(
        claims,
        any_experiment_done,
        refutations_needed=refutations_needed,
        refutation_priority=refutation_priority,
        supported_needed=supported_needed,
        confirmations_needed=k,
    )
    return v, by_lit


def excluded_confirmations_note(
    claims: Sequence[Any],
    *,
    verdict_value: str,
) -> str | None:
    """PR8 priority-refuted note: stub/partial supporting results excluded."""
    if verdict_value != "refuted":
        return None
    stub_partial = []
    for c in claims:
        if getattr(c, "provenance", "") not in ("stub", "partial"):
            continue
        oc = effective_outcome(c)
        try:
            stance = claim_stance(str(getattr(c, "criterion_status", "unassessed")))
        except KeyError:
            stance = "inconclusive"
        if oc == "consistent" or stance == "supports":
            stub_partial.append(c)
    if not stub_partial:
        return None
    keys = {
        getattr(c, "attempt_id", "") or getattr(c, "artifact_id", "") or id(c)
        for c in stub_partial
    }
    keys.discard("")
    n = len(keys) if keys else len(stub_partial)
    if n <= 0:
        return None
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
    """SPEC hypotheses[] item (also used as hypothesis_row)."""
    counted_rows: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    supporting: list[str] = []
    refuting: list[str] = []
    aux: list[str] = []
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
            role = getattr(c, "check_role", None)
            if role == "validity" and getattr(c, "validity_ok", True):
                tid = getattr(c, "template_id", None) or "validity"
                if tid not in aux:
                    aux.append(str(tid))
        else:
            excluded_rows.append(
                {"claim_id": cid, "reason": exclusion_reason(c) or "invalid"}
            )
    conflict = None
    if verdict_value == "refuted" and supporting and refuting:
        conflict = {"supporting": supporting, "refuting": refuting}
    severity_note = None
    if verdict_value in ("survived", "supported"):
        severity_note = (
            "survived != proven; tests that could have refuted: "
            + ", ".join(
                r["claim_id"]
                for r in counted_rows
                if r.get("intent") == "refute" or r.get("severe")
            )
        )
    elif verdict_value == "refuted" and supporting:
        severity_note = (
            f"refuted despite {len(supporting)} supporting claims (conflict)"
        )
    if excluded_rows:
        reasons: dict[str, int] = {}
        for row in excluded_rows:
            r = str(row.get("reason") or "invalid")
            reasons[r] = reasons.get(r, 0) + 1
        excl_txt = (
            f"{sum(reasons.values())} claims excluded ("
            + ", ".join(f"{r} {n}" for r, n in sorted(reasons.items()))
            + ")"
        )
        severity_note = f"{severity_note}; {excl_txt}" if severity_note else excl_txt
    return {
        "hypothesis_id": hypothesis_id,
        "question_id": question_id,
        "verdict": verdict_value,
        "rule": rule or "no_counted",
        "counted": counted_rows,
        "excluded": excluded_rows,
        "conflict": conflict,
        "severity_note": severity_note,
        "auxiliary_assumptions": aux,
        "legacy_claims": legacy_claims,
    }


# Alias required by review wording.
hypothesis_row = hypothesis_report_row


def disposition_for_question(
    *,
    hypothesis_verdicts: Mapping[str, str],
    question_verdict: str | None,
    has_hypotheses: bool,
) -> str:
    """SPEC disposition mapping (with/without hypotheses)."""
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
