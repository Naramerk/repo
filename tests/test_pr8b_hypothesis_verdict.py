"""M1 PR8b: hypothesis verdict model (DESIGN_HYP_DELTA)."""

from __future__ import annotations

from coscientist.core.claims import RuntimeClaim, excluded_claims_report
from coscientist.core.rules import verdict
from coscientist.core.upcasters import upcast
from coscientist.core.verdict import (
    claim_is_severe,
    exclusion_reason,
    hypothesis_report_row,
    hypothesis_verdict,
)


def _c(**kw) -> RuntimeClaim:
    base = dict(
        criterion_status="met",
        accepted=True,
        provenance="real",
        frozen=True,
        evidence_kind="computational",
        attempt_id="A1",
        artifact_id="Art1",
        validity_ok=True,
        check_role="prediction",
        claim_outcome="consistent",
        severe=True,
        independence_key="k1",
        intent="confirm",
    )
    base.update(kw)
    return RuntimeClaim(**base)  # type: ignore[arg-type]


def test_verdict_untested_no_claims():
    assert hypothesis_verdict([], False)[0] == "untested"


def test_stub_partial_excluded_before_counting():
    stub = _c(provenance="stub", attempt_id="S1", independence_key="s1")
    real_ref = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        attempt_id="R1",
        independence_key="r1",
        intent="confirm",
    )
    assert not stub.counts()
    assert exclusion_reason(stub) == "stub"
    v, _, rule = hypothesis_verdict(
        [stub, real_ref],
        True,
        refutations_needed=1,
        refutation_priority=True,
    )
    assert v == "refuted" and rule == "k_ref"
    # Removing stub leaves verdict unchanged (V2).
    v2, _, _ = hypothesis_verdict(
        [real_ref], True, refutations_needed=1, refutation_priority=True
    )
    assert v2 == "refuted"


def test_validity_failure_never_refutes():
    bad = _c(
        validity_ok=False,
        claim_outcome=None,
        severe=False,
        criterion_status="unassessed",
        exclusion_reason="validity_failed",
        check_role="validity",
    )
    assert not bad.counts()
    v, _ = verdict([bad], True, refutation_priority=True)
    assert v == "inconclusive"


def test_lit_not_counted():
    lit = _c(evidence_kind="literature", lit_own_criterion=True, independence_key="L")
    assert not lit.counts()
    assert exclusion_reason(lit) == "lit"


def test_refuted_k_ref_1():
    ref = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="r",
    )
    assert hypothesis_verdict([ref], True, refutations_needed=1, refutation_priority=True)[
        0
    ] == "refuted"


def test_refutation_priority_false_conflict_inconclusive():
    ref = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="r",
        attempt_id="R",
    )
    con = _c(independence_key="c", attempt_id="C", intent="refute")
    v, _, rule = hypothesis_verdict(
        [ref, con],
        True,
        refutations_needed=1,
        refutation_priority=False,
    )
    assert v == "inconclusive" and rule == "k_ref"


def test_survived_single_severe():
    one = _c(intent="confirm", independence_key="a")
    assert claim_is_severe(one)
    assert hypothesis_verdict([one], True, min_independent_support=2)[0] == "survived"


def test_supported_needs_two_independent_one_refute_intent():
    a = _c(intent="refute", independence_key="a", attempt_id="A")
    b = _c(intent="confirm", independence_key="b", attempt_id="B", artifact_id="Art2")
    assert (
        hypothesis_verdict([a, b], True, min_independent_support=2)[0] == "supported"
    )
    # Same independence_key → counts once → survived not supported.
    dup = _c(intent="refute", independence_key="a", attempt_id="A2", artifact_id="Art3")
    assert (
        hypothesis_verdict([a, dup], True, min_independent_support=2)[0] == "survived"
    )


def test_refuted_with_confirmations_reports_conflict_note():
    from coscientist.core.rules import priority_refuted_support_note

    ref = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="r",
        attempt_id="R",
    )
    stub_sup = _c(
        provenance="stub",
        independence_key="s",
        attempt_id="S",
        artifact_id="AS",
    )
    note = priority_refuted_support_note(
        [ref, stub_sup],
        True,
        refutations_needed=1,
        refutation_priority=True,
    )
    assert note is not None and "stub" in note


def test_upcaster_studycreated_v4_to_v5_defaults():
    v, out = upcast("StudyCreated", 4, {"study_id": "s"})
    assert v == 5
    assert out["min_independent_support"] == 2
    assert out["aggregate"] == "count"
    assert out["evalue_alpha"] == 0.05


def test_report_hypotheses_payload_schema():
    c = _c(node_id="n1", intent="refute")
    row = hypothesis_report_row(
        hypothesis_id="H1",
        question_id="Q1",
        claims=[c],
        verdict_value="survived",
        rule="single_severe",
    )
    assert row["hypothesis_id"] == "H1"
    assert row["counted"] and row["counted"][0]["severe"] is True
    assert row["severity_note"]


def test_excluded_claims_report_lists_stub():
    stub = _c(provenance="stub", node_id="n", attempt_id="S")
    excl = excluded_claims_report([stub])
    assert excl and excl[0]["reason"] == "stub"
