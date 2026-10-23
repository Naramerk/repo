"""Hypothesis verdict model (M1 PR8b / DESIGN_HYP_DELTA).

Pure functions: claim classification, counting filter, hypothesis ``verdict()``,
and report payload builders.

Deviations (see PR body / CHANGELOG):
- ``VerdictSnapshotsRecorded`` kept for bind; finalize recomputes report rows.
- StudyCreated is v6 (``supported_needed``; confirmations_needed remains PR8 ``k``).
- validity_failed / unknown_role per-experiment exclusion from PR8 kept.
- Real journal events use GraphNodeUpserted (claims), not ClaimRecorded.
- E-value aggregation deferred (``aggregate='evalue'`` rejected at StudyCreated).
- R17 runtime metadata still deferred (PR8).
- SESOI / predicts_null from catalog template only (not RequirementRef.params).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol

from coscientist.core.digest import canonical_json, spec_digest
from coscientist.core.r17 import claim_counts_under_r17
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
PredictedDirection = Literal["positive", "negative", "two_sided"]


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


def artifact_is_data(*, role: str | None, media_type: str | None) -> bool:
    """Classify artifact as data vs code from core role/media_type (not input name)."""
    if role in ("code", "log", "report"):
        return False
    if role in ("data", "paper", "table", "figure"):
        return True
    mt = (media_type or "").lower()
    if any(
        tok in mt
        for tok in (
            "python",
            "javascript",
            "typescript",
            "x-sh",
            "x-shellscript",
            "x-script",
            "x-bytecode",
        )
    ):
        return False
    if mt.startswith("text/x-"):
        return False
    return True


def data_digests_from_bindings(
    bindings: Sequence[Any] | None,
    *,
    artifact_lookup: Callable[[str], Any] | None = None,
) -> list[str]:
    """Content digests of **data** artifact bindings (code ignored).

    Classification uses Artifact.role / media_type from the core store when
    ``artifact_lookup`` is provided — never the planner-chosen input name.
    """
    out: list[str] = []
    for b in bindings or ():
        if isinstance(b, Mapping):
            kind = b.get("kind", "artifact")
            aid = str(b.get("artifact_id") or "")
            digest = b.get("digest")
        else:
            kind = getattr(b, "kind", "artifact")
            aid = str(getattr(b, "artifact_id", "") or "")
            digest = getattr(b, "digest", None)
        if kind != "artifact":
            continue
        role: str | None = None
        media_type: str | None = None
        content: str | None = None
        if aid and artifact_lookup is not None:
            meta = artifact_lookup(aid)
            if meta is not None:
                role = getattr(meta, "role", None)
                media_type = getattr(meta, "media_type", None)
                content = getattr(meta, "sha256", None) or getattr(meta, "digest", None)
        if content is None and digest:
            # No store meta: treat binding digest as content; role unknown → data.
            content = str(digest)
        if not content:
            continue
        if artifact_is_data(role=role, media_type=media_type):
            out.append(str(content))
    return sorted(set(out))


# Back-compat alias used by older tests/imports.
def digests_from_bindings(
    bindings: Sequence[Any] | None,
    *,
    artifact_lookup: Callable[[str], Any] | None = None,
) -> tuple[str | None, str | None]:
    """Deprecated shape: (folded_data, None). Prefer ``data_digests_from_bindings``."""
    data = data_digests_from_bindings(bindings, artifact_lookup=artifact_lookup)
    if not data:
        return None, None
    if len(data) == 1:
        return data[0], None
    return hashlib.sha256(canonical_json(data)).hexdigest(), None


def normalized_task_spec_digest(spec: Any) -> str:
    """Hash of TaskSpec excluding identity fields (task_key)."""
    if hasattr(spec, "model_dump"):
        raw = spec.model_dump(mode="json")
    elif isinstance(spec, Mapping):
        raw = dict(spec)
    else:
        raw = {}
    raw.pop("task_key", None)
    return spec_digest(raw)


def independence_key_for(*, experiment_id: str | None = None) -> str:
    """Base independence key: sha256(JCS({experiment_id})). Method is not in the key."""
    return hashlib.sha256(
        canonical_json({"experiment_id": experiment_id or ""})
    ).hexdigest()


def _claim_data_digests(claim: Any) -> frozenset[str]:
    raw = getattr(claim, "data_digests", None) or ()
    return frozenset(str(x) for x in raw if x)


def _claim_spec_group(claim: Any) -> tuple[str, str] | None:
    sd = getattr(claim, "spec_digest", None) or ""
    if not sd:
        return None
    plan = str(getattr(claim, "plan_id", None) or getattr(claim, "study_id", None) or "")
    return (plan, str(sd))


def _union_find_parent(parent: dict[str, str], x: str) -> str:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _union_find_merge(parent: dict[str, str], a: str, b: str) -> None:
    ra, rb = _union_find_parent(parent, a), _union_find_parent(parent, b)
    if ra != rb:
        parent[rb] = ra


def source_id_for_claim(claim: Any) -> str:
    """Effective independence source after overlap / identical-spec merge rules.

    - Base: ``independence_key`` (experiment_id-derived).
    - Merge experiments whose bound **data** digests overlap.
    - No-input experiments: same plan/study + identical normalized spec → one source.
    """
    # distinct_by_key computes components; this helper is for single-claim fallback.
    return str(
        getattr(claim, "independence_key", None)
        or independence_key_for(
            experiment_id=getattr(claim, "experiment_id", None)
            or getattr(claim, "attempt_id", "")
            or "x"
        )
    )


def distinct_by_key(claims: Sequence[Any]) -> list[Any]:
    """Keep first claim per independence source (overlap-aware)."""
    items = list(claims)
    if not items:
        return []

    # Node id per claim for union-find.
    ids = [f"c{i}" for i in range(len(items))]
    parent = {i: i for i in ids}

    # Index digests → claim ids for overlap unions.
    digest_owners: dict[str, list[str]] = {}
    spec_owners: dict[tuple[str, str], list[str]] = {}
    for cid, c in zip(ids, items, strict=True):
        dig_set = _claim_data_digests(c)
        if dig_set:
            for d in dig_set:
                digest_owners.setdefault(d, []).append(cid)
        else:
            grp = _claim_spec_group(c)
            if grp is not None:
                spec_owners.setdefault(grp, []).append(cid)

    for owners in digest_owners.values():
        for other in owners[1:]:
            _union_find_merge(parent, owners[0], other)
    # Overlap across digests: if A has {d1,d2} and B has {d2,d3}, both listed under d2.
    # Also connect claims that share any digest via transitive closure of per-digest unions.
    for cid, c in zip(ids, items, strict=True):
        dig_list = list(_claim_data_digests(c))
        for i in range(1, len(dig_list)):
            a = digest_owners[dig_list[0]][0]
            b = digest_owners[dig_list[i]][0]
            _union_find_merge(parent, a, b)
            _union_find_merge(parent, cid, a)

    for owners in spec_owners.values():
        for other in owners[1:]:
            _union_find_merge(parent, owners[0], other)

    # Claims with neither digests nor spec_digest: singleton by independence_key.
    key_owners: dict[str, list[str]] = {}
    for cid, c in zip(ids, items, strict=True):
        if _claim_data_digests(c) or _claim_spec_group(c) is not None:
            continue
        key_owners.setdefault(source_id_for_claim(c), []).append(cid)
    for owners in key_owners.values():
        for other in owners[1:]:
            _union_find_merge(parent, owners[0], other)

    seen: set[str] = set()
    out: list[Any] = []
    for cid, c in zip(ids, items, strict=True):
        root = _union_find_parent(parent, cid)
        if root in seen:
            continue
        seen.add(root)
        out.append(c)
    return out


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


def effect_exceeds_sesoi(
    *,
    sesoi: float | None,
    effect: Mapping[str, Any] | None,
    direction: PredictedDirection | str | None = "two_sided",
) -> bool:
    """Non-null prediction: core effect exceeds sesoi in the predicted direction."""
    if sesoi is None or effect is None:
        return False
    try:
        est = float(effect["est"])  # type: ignore[index]
        s = float(sesoi)
    except (KeyError, TypeError, ValueError):
        return False
    d = direction or "two_sided"
    if d == "positive":
        return est > s
    if d == "negative":
        return est < -s
    return abs(est) > s


def claim_is_severe(claim: Any) -> bool:
    """Severe: consistent ∧ valid ∧ frozen ∧ catalog uses_sesoi ∧ check used sesoi ∧ effect gate.

    ``min_power`` removed (nothing computes power in M1). Explicit ``severe=False``
    is definitive (V5 legacy). Explicit ``severe=True`` from the core stamp is
    honored after the base gates (executor never stamps True without the gate).
    """
    if effective_outcome(claim) != "consistent":
        return False
    if effective_validity(claim) != "valid":
        return False
    # Legacy / explicit non-severe stamp is definitive (V5).
    if getattr(claim, "severe", None) is False:
        return False
    if not bool(getattr(claim, "frozen", False)):
        return False
    frozen_at = getattr(claim, "criterion_frozen_at_seq", None)
    first_ev = getattr(claim, "first_cited_evidence_seq", None)
    if frozen_at is not None and first_ev is not None and not (frozen_at < first_ev):
        return False
    # Core-stamped True is authoritative (set only after the gate in executor).
    if getattr(claim, "severe", None) is True:
        return True
    sesoi = getattr(claim, "sesoi", None)
    uses = getattr(claim, "uses_sesoi", None)
    if uses is False:
        return False
    if uses is not True and sesoi is None:
        return False
    if sesoi is None:
        return False
    used = getattr(claim, "used_sesoi", None)
    if used is False:
        return False
    effect = getattr(claim, "effect", None)
    predicts_null = bool(getattr(claim, "predicts_null", False))
    if predicts_null:
        return tost_holds(sesoi=float(sesoi), effect=effect)
    direction = getattr(claim, "predicted_direction", None) or "two_sided"
    return effect_exceeds_sesoi(
        sesoi=float(sesoi), effect=effect, direction=direction
    )


def refute_intent_counts(claim: Any) -> bool:
    """Refute intent counts toward supported only if the prediction could fail.

    Requires: intent=refute, catalog uses_sesoi, frozen, prediction role, check used sesoi.
    A planner label alone is not enough.
    """
    if _intent(claim) != "refute":
        return False
    if getattr(claim, "refute_capable", None) is False:
        return False
    if getattr(claim, "refute_capable", None) is True:
        return True
    if not bool(getattr(claim, "frozen", False)):
        return False
    if getattr(claim, "check_role", "unknown") != "prediction":
        return False
    uses = getattr(claim, "uses_sesoi", None)
    if uses is False:
        return False
    if getattr(claim, "sesoi", None) is None and uses is not True:
        return False
    used = getattr(claim, "used_sesoi", None)
    if used is False:
        return False
    return True


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


def _intent(claim: Any) -> TestsIntent:
    intent = getattr(claim, "intent", None) or DEFAULT_TESTS_INTENT
    if intent not in ("confirm", "refute"):
        return DEFAULT_TESTS_INTENT
    return intent  # type: ignore[return-value]


def claim_counts_for_verdict(claim: Any) -> bool:
    """Base ``counts()`` plus R17 when freeze/evidence metadata is present.

    Lives here (not ``rules``) so ``rules``↔``verdict`` has no import cycle.
    """
    fn = getattr(claim, "counts_under_r17", None)
    if callable(fn):
        return bool(fn())
    base = getattr(claim, "counts", None)
    if callable(base):
        if not base():
            return False
    else:
        return False
    try:
        stance = claim_stance(str(getattr(claim, "criterion_status", "unassessed")))
    except KeyError:
        stance = "inconclusive"
    if stance == "refutes":
        return True
    frozen_at = getattr(claim, "criterion_frozen_at_seq", None)
    first_ev = getattr(claim, "first_cited_evidence_seq", None)
    confirming = getattr(claim, "confirming_evidence_ids", None)
    threshold_src = getattr(claim, "threshold_source_evidence_ids", None)
    if frozen_at is None and first_ev is None and confirming is None:
        return True
    return claim_counts_under_r17(
        criterion_frozen_at_seq=frozen_at,
        first_cited_evidence_seq=first_ev,
        confirming_evidence_ids=set(confirming or ()),
        threshold_source_evidence_ids=set(threshold_src or ()),
        stance=stance,
    )


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

    counted = [c for c in about if claim_counts_for_verdict(c)]

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
        if claim_counts_for_verdict(c):
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
