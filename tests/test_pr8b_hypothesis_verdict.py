"""M1 PR8b: hypothesis verdict model (DESIGN_HYP_DELTA)."""

from __future__ import annotations

from hypothesis import HealthCheck, Phase, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

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
    assert hypothesis_verdict([one], True, confirmations_needed=2)[0] == "survived"


def test_supported_needs_two_independent_one_refute_intent():
    a = _c(intent="refute", independence_key="a", attempt_id="A")
    b = _c(intent="confirm", independence_key="b", attempt_id="B", artifact_id="Art2")
    assert (
        hypothesis_verdict([a, b], True, confirmations_needed=2)[0] == "supported"
    )
    # Same independence_key → counts once → survived not supported.
    dup = _c(intent="refute", independence_key="a", attempt_id="A2", artifact_id="Art3")
    assert (
        hypothesis_verdict([a, dup], True, confirmations_needed=2)[0] == "survived"
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
    assert out["aggregate"] == "count"
    assert out["evalue_alpha"] == 0.05
    assert "min_independent_support" not in out


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


def test_null_claim_requires_tost():
    from coscientist.core.verdict import classify_outcome, tost_holds

    assert not tost_holds(sesoi=None, effect={"ci_low": -0.1, "ci_high": 0.1})
    assert tost_holds(sesoi=1.0, effect={"ci_low": -0.1, "ci_high": 0.1})
    assert not tost_holds(sesoi=0.05, effect={"ci_low": -0.1, "ci_high": 0.1})
    v, oc, _ = classify_outcome(
        validity_ok=True,
        prediction_statuses=["PASSED"],
        predicts_null=True,
        sesoi=None,
        effect={"ci_low": -0.1, "ci_high": 0.1},
    )
    assert v == "valid" and oc == "indeterminate"


def test_underpowered_is_indeterminate_exclusion():
    c = _c(
        claim_outcome="indeterminate",
        criterion_status="unassessed",
        severe=False,
        exclusion_reason="indeterminate",
    )
    assert not c.counts()
    assert exclusion_reason(c) == "indeterminate"


def test_severe_requires_frozen_sesoi_or_power():
    from coscientist.core.verdict import claim_is_severe

    bare = _c(severe=None, sesoi=None, min_power=None, frozen=True)
    assert claim_is_severe(bare) is False
    with_power = _c(severe=None, sesoi=None, min_power=0.0, power=0.5, frozen=True)
    assert claim_is_severe(with_power) is True


def test_duplicate_runs_count_once():
    a = _c(intent="refute", independence_key="same", attempt_id="A1", artifact_id="A1")
    b = _c(intent="refute", independence_key="same", attempt_id="A2", artifact_id="A2")
    assert hypothesis_verdict([a, b], True, confirmations_needed=2)[0] == "survived"


def test_refuted_k_ref_2_needs_independent():
    r1 = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="r1",
        attempt_id="R1",
    )
    r2 = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="r2",
        attempt_id="R2",
        artifact_id="Art2",
    )
    assert (
        hypothesis_verdict([r1], True, refutations_needed=2, refutation_priority=True)[0]
        == "inconclusive"
    )
    assert (
        hypothesis_verdict(
            [r1, r2], True, refutations_needed=2, refutation_priority=True
        )[0]
        == "refuted"
    )


def test_inc_below_k_ref_inconclusive():
    r1 = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="r1",
    )
    v, _, rule = hypothesis_verdict(
        [r1], True, refutations_needed=2, refutation_priority=True
    )
    assert v == "inconclusive" and rule == "inc_below_k_ref"


def test_disposition_mapping_with_hypotheses():
    from coscientist.core.verdict import disposition_for_question

    assert (
        disposition_for_question(
            hypothesis_verdicts={"H1": "supported", "H2": "refuted"},
            question_verdict=None,
            has_hypotheses=True,
        )
        == "answered"
    )
    assert (
        disposition_for_question(
            hypothesis_verdicts={"H1": "survived", "H2": "inconclusive"},
            question_verdict=None,
            has_hypotheses=True,
        )
        == "survived"
    )
    assert (
        disposition_for_question(
            hypothesis_verdicts={"H1": "refuted", "H2": "refuted"},
            question_verdict=None,
            has_hypotheses=True,
        )
        == "refuted"
    )


def test_evalue_product_threshold():
    from coscientist.core.verdict import evalue_verdict

    refs = [
        _c(
            criterion_status="not_met",
            claim_outcome="inconsistent",
            severe=False,
            independence_key=f"r{i}",
            attempt_id=f"R{i}",
            artifact_id=f"A{i}",
            p_value=0.01,
        )
        for i in range(3)
    ]
    assert evalue_verdict(refs, evalue_alpha=0.05) == "refuted"


def test_evalue_shared_key_max_only():
    from coscientist.core.verdict import evalue_verdict

    a = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="same",
        attempt_id="R1",
        p_value=0.01,
    )
    b = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="same",
        attempt_id="R2",
        artifact_id="A2",
        p_value=0.001,
    )
    # Shared key → max only; calibrate(0.001)≈15.8 < 1/0.05=20 → inconclusive.
    assert evalue_verdict([a, b], evalue_alpha=0.05) == "inconclusive"


def test_r17_violation_invalid():
    from coscientist.core.verdict import classify_outcome

    v, oc, _ = classify_outcome(
        validity_ok=True,
        prediction_statuses=["PASSED"],
        r17_ok=False,
    )
    assert v == "invalid" and oc is None


def test_verdict_replay_equals_finalized():
    """V1: recomputed verdict matches a persisted snapshot for the same claims."""
    claims = [
        _c(intent="refute", independence_key="a", attempt_id="A"),
        _c(
            intent="confirm",
            independence_key="b",
            attempt_id="B",
            artifact_id="Art2",
        ),
    ]
    v1, by1, rule1 = hypothesis_verdict(claims, True, confirmations_needed=2)
    snap = {"verdict": v1, "by_literature": by1, "rule": rule1}
    v2, by2, rule2 = hypothesis_verdict(claims, True, confirmations_needed=2)
    assert (v2, by2, rule2) == (snap["verdict"], snap["by_literature"], snap["rule"])
    assert v1 == "supported"


def test_legacy_claims_never_supported():
    legacy = _c(severe=False, claim_outcome="consistent", intent="refute")
    other = _c(
        severe=False,
        claim_outcome="consistent",
        intent="confirm",
        independence_key="b",
        attempt_id="B",
        artifact_id="B",
    )
    # Upcasted/legacy: severe=false → cannot reach supported.
    assert (
        hypothesis_verdict([legacy, other], True, confirmations_needed=2)[0]
        == "inconclusive"
    )


def test_minimal_report_hypotheses_truncation_le_10kb():
    from coscientist.core.digest import canonical_json
    from coscientist.core.domain import Task, TaskKind, TaskOutcome, TaskSpec, TaskState
    from coscientist.core.minimal_report import MAX_BYTES, build_minimal_report

    tasks = {
        "st/T0": Task(
            task_id="st/T0",
            kind=TaskKind.EXPERIMENT,
            spec=TaskSpec(task_key="T0", title="t", goal="g", route="experiment"),
            state=TaskState.DONE,
            outcome=TaskOutcome.SUCCESS,
            deadline=__import__("datetime").datetime(2026, 1, 1),
        )
    }
    hyps = [
        {
            "hypothesis_id": f"H{i}",
            "question_id": "Q1",
            "verdict": "survived",
            "rule": "single_severe",
            "counted": [
                {
                    "claim_id": f"c{i}",
                    "outcome": "consistent",
                    "intent": "confirm",
                    "severe": True,
                    "independence_key": f"k{i}",
                }
            ],
            "excluded": [],
            "conflict": None,
            "severity_note": "x" * 200,
            "auxiliary_assumptions": [],
            "legacy_claims": 0,
            "e_values": None,
        }
        for i in range(80)
    ]
    report = build_minimal_report(
        study_id="st",
        terminal_reason="completed",
        trigger="all_done",
        contributing_reasons=[],
        verdict="partial",
        tasks=tasks,
        attempts={},
        question_dispositions={
            "Q1": {
                "disposition": "survived",
                "reason": "",
                "formulation": "q",
            }
        },
        fallback_reason=None,
        hypotheses=hyps,
    )
    assert len(canonical_json(report)) <= MAX_BYTES
    assert report.get("hypotheses") is not None


# --- Property: record_claim + invariants V1–V5 --------------------------------


class VerdictPropertyMachine(RuleBasedStateMachine):
    """record_claim(validity, outcome, provenance, key) with V1–V5."""

    def __init__(self):
        super().__init__()
        self.claims: list[RuntimeClaim] = []
        self._n = 0
        self.k_ref = 1
        self.k_sup = 2
        self.priority = True
        self._last: tuple[str, bool, str | None] | None = None

    def _verdict(self, claims=None):
        return hypothesis_verdict(
            claims if claims is not None else self.claims,
            True,
            refutations_needed=self.k_ref,
            refutation_priority=self.priority,
            confirmations_needed=self.k_sup,
        )

    @rule(
        validity=st.sampled_from(["valid", "invalid"]),
        outcome=st.sampled_from(
            ["consistent", "inconsistent", "indeterminate", None]
        ),
        provenance=st.sampled_from(["real", "partial", "stub"]),
        key=st.sampled_from(["k0", "k1", "k2"]),
        lit=st.booleans(),
        intent=st.sampled_from(["confirm", "refute"]),
        severe=st.booleans(),
        legacy=st.booleans(),
    )
    def record_claim(self, validity, outcome, provenance, key, lit, intent, severe, legacy):
        self._n += 1
        if validity == "invalid":
            outcome = None
            severe = False
        if outcome is None and validity == "valid":
            outcome = "indeterminate"
        status = {
            "consistent": "met",
            "inconsistent": "not_met",
            "indeterminate": "unassessed",
            None: "unassessed",
        }[outcome]
        # Legacy stamps are never severe (V5).
        if legacy:
            severe = False
        c = _c(
            criterion_status=status,
            claim_outcome=outcome,
            validity_ok=(validity == "valid"),
            validity=validity,
            provenance=provenance,
            independence_key=key,
            attempt_id=f"A{self._n}",
            artifact_id=f"Art{self._n}",
            evidence_kind="literature" if lit else "computational",
            lit_own_criterion=lit,
            intent=intent,
            severe=severe if outcome == "consistent" and validity == "valid" else False,
            min_power=0.0 if not legacy else None,
            exclusion_reason=(
                "validity_failed"
                if validity == "invalid"
                else ("lit" if lit else ("stub" if provenance == "stub" else None))
            ),
        )
        self.claims.append(c)
        self._last = self._verdict()

    @invariant()
    def v1_deterministic(self):
        a = self._verdict()
        b = self._verdict()
        assert a == b
        if self._last is not None:
            assert a == self._last

    @invariant()
    def v2_excluded_irrelevant(self):
        # V2: dropping stub/partial/lit/invalid changes no scientific verdict.
        # Edge: only-noise bag is inconclusive; empty bag is untested — both mean
        # no counted evidence (SPEC §5 "no claims" = empty list).
        filtered = []
        for c in self.claims:
            if c.provenance in ("stub", "partial"):
                continue
            if c.is_lit:
                continue
            if getattr(c, "exclusion_reason", None) == "invalid":
                continue
            filtered.append(c)
        full_v = self._verdict()[0]
        filt_v = self._verdict(filtered)[0]
        if not filtered:
            assert full_v in ("untested", "inconclusive")
            assert filt_v == "untested"
        else:
            assert full_v == filt_v

    @invariant()
    def v3_supported_needs_keys_and_refute(self):
        v, _, _ = self._verdict()
        if v != "supported":
            return
        counted = [c for c in self.claims if c.counts()]
        severe = [
            c
            for c in counted
            if c.claim_outcome == "consistent" and claim_is_severe(c)
        ]
        keys = {c.independence_key for c in severe}
        assert len(keys) >= self.k_sup
        assert any(c.intent == "refute" for c in severe)

    @invariant()
    def v4_refuted_needs_k_ref_not_validity_alone(self):
        v, _, _ = self._verdict()
        if v != "refuted":
            return
        counted = [c for c in self.claims if c.counts()]
        keys = {
            c.independence_key
            for c in counted
            if c.claim_outcome == "inconsistent"
        }
        assert len(keys) >= self.k_ref
        # Validity failure alone never refutes.
        if not any(c.claim_outcome == "inconsistent" and c.counts() for c in self.claims):
            raise AssertionError("refuted without inconsistent counted claims")

    @invariant()
    def v5_legacy_never_supported(self):
        v, _, _ = self._verdict()
        if v != "supported":
            return
        # Every supporting severe claim must not be a legacy (severe=False) stamp.
        counted = [c for c in self.claims if c.counts() and claim_is_severe(c)]
        assert counted and all(c.severe is not False for c in counted)


def test_verdict_property_record_claim_v1_v5():
    run_state_machine_as_test(
        VerdictPropertyMachine,
        settings=settings(
            max_examples=40,
            stateful_step_count=15,
            deadline=None,
            suppress_health_check=[HealthCheck.too_slow],
            phases=[Phase.generate, Phase.target, Phase.shrink],
        ),
    )
