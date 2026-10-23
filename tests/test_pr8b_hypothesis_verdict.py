"""M1 PR8b: hypothesis verdict model (DESIGN_HYP_DELTA) — review fixes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import HealthCheck, Phase, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

from coscientist.core.claims import RuntimeClaim, filtered_claims_by_target
from coscientist.core.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    InputSpec,
    OutputSpec,
    QuestionSpec,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.rules import verdict
from coscientist.core.upcasters import upcast
from coscientist.core.verdict import (
    claim_is_severe,
    data_digests_from_bindings,
    distinct_by_key,
    exclusion_reason,
    hypothesis_report_row,
    hypothesis_verdict,
    independence_key_for,
    normalized_task_spec_digest,
    refute_intent_counts,
)
from tests.helpers import attach_executor, close_runtime, open_runtime


def _c(**kw) -> RuntimeClaim:
    """Severe-capable claim helper (catalog sesoi + effect exceeding it)."""
    base = dict(
        criterion_status="met",
        accepted=True,
        provenance="real",
        frozen=True,
        evidence_kind="computational",
        attempt_id="A1#1",
        artifact_id="Art1",
        validity_ok=True,
        check_role="prediction",
        claim_outcome="consistent",
        severe=True,
        independence_key="k1",
        experiment_id="A1",
        intent="confirm",
        sesoi=0.5,
        uses_sesoi=True,
        used_sesoi=True,
        predicts_null=False,
        predicted_direction="positive",
        effect={"est": 1.5, "ci_low": 1.5, "ci_high": 1.5},
        refute_capable=True,
    )
    base.update(kw)
    return RuntimeClaim(**base)  # type: ignore[arg-type]


def test_verdict_untested_no_claims():
    assert hypothesis_verdict([], False)[0] == "untested"


def test_stub_partial_excluded_before_counting():
    stub = _c(provenance="stub", attempt_id="S1#1", independence_key="s1", severe=False)
    real_ref = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        attempt_id="R1#1",
        independence_key="r1",
        sesoi=None,
        refute_capable=False,
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
        sesoi=None,
        refute_capable=False,
    )
    assert not bad.counts()
    v, _ = verdict([bad], True, refutation_priority=True)
    assert v == "inconclusive"


def test_lit_not_counted():
    lit = _c(
        evidence_kind="literature",
        lit_own_criterion=True,
        independence_key="L",
        severe=False,
        sesoi=None,
    )
    assert not lit.counts()
    assert exclusion_reason(lit) == "lit"


def test_only_excluded_is_inconclusive_not_untested():
    stub = _c(provenance="stub", severe=False, sesoi=None, refute_capable=False)
    assert hypothesis_verdict([stub], True)[0] == "inconclusive"


def test_refuted_k_ref_1():
    ref = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="r",
        sesoi=None,
        refute_capable=False,
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
        sesoi=None,
        refute_capable=False,
    )
    con = _c(independence_key="c", attempt_id="C#1", experiment_id="C", intent="refute")
    assert (
        hypothesis_verdict(
            [con, ref], True, refutations_needed=1, refutation_priority=False
        )[0]
        == "inconclusive"
    )


def test_survived_single_severe():
    """Defaults: supported_needed=2 → one severe is survived, never supported."""
    one = _c(intent="refute")
    assert hypothesis_verdict([one], True, supported_needed=2)[0] == "survived"


def test_supported_needs_two_independent_one_refute_intent():
    a = _c(intent="refute", independence_key="a", attempt_id="A#1", experiment_id="A")
    b = _c(
        intent="confirm",
        independence_key="b",
        attempt_id="B#1",
        experiment_id="B",
        artifact_id="Art2",
    )
    assert (
        hypothesis_verdict([a, b], True, supported_needed=2)[0] == "supported"
    )
    dup = _c(
        intent="refute",
        independence_key="a",
        attempt_id="A#2",
        experiment_id="A",
        artifact_id="Art3",
    )
    assert (
        hypothesis_verdict([a, dup], True, supported_needed=2)[0] == "survived"
    )


def test_refute_intent_needs_failable_prediction():
    """Planner label alone is not enough for supported (item 8)."""
    # Force severe via explicit stamp with sesoi for counting as severe con:
    a2 = _c(
        intent="refute",
        independence_key="a",
        attempt_id="A#1",
        experiment_id="A",
        refute_capable=False,
        sesoi=0.5,
        severe=True,
    )
    b = _c(
        intent="confirm",
        independence_key="b",
        attempt_id="B#1",
        experiment_id="B",
        artifact_id="Art2",
        refute_capable=True,
    )
    assert not refute_intent_counts(a2)
    assert hypothesis_verdict([a2, b], True, supported_needed=2)[0] == "survived"


def test_refuted_with_confirmations_reports_conflict_note():
    from coscientist.core.verdict import excluded_confirmations_note

    stub = _c(
        provenance="stub",
        attempt_id="S#1",
        independence_key="s",
        severe=False,
        sesoi=None,
    )
    ref = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="r",
        attempt_id="R#1",
        sesoi=None,
        refute_capable=False,
    )
    note = excluded_confirmations_note([stub, ref], verdict_value="refuted")
    assert note == "refuted; 1 supporting results were stub/partial"


def test_upcaster_studycreated_v5_to_v6_supported_needed():
    v, out = upcast("StudyCreated", 5, {"study_id": "s"})
    assert v == 6
    assert out["supported_needed"] == 2
    assert out.get("aggregate", "count") == "count"


def test_upcaster_studycreated_v4_to_v5_defaults():
    v, out = upcast("StudyCreated", 4, {"study_id": "s", "confirmations_needed": 1})
    assert v == 6
    assert out["aggregate"] == "count"
    assert out["supported_needed"] == 2
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
    for key in (
        "hypothesis_id",
        "question_id",
        "verdict",
        "rule",
        "counted",
        "excluded",
        "conflict",
        "severity_note",
        "auxiliary_assumptions",
        "legacy_claims",
    ):
        assert key in row


def test_null_claim_requires_tost():
    from coscientist.core.verdict import classify_outcome, tost_holds

    assert not tost_holds(sesoi=None, effect={"ci_low": -0.1, "ci_high": 0.1})
    assert tost_holds(sesoi=1.0, effect={"ci_low": -0.1, "ci_high": 0.1})
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
        sesoi=None,
    )
    assert not c.counts()
    assert exclusion_reason(c) == "indeterminate"


def test_severe_requires_catalog_sesoi_and_effect():
    """Severe needs uses_sesoi + used_sesoi + effect exceeding sesoi (no min_power)."""
    bare = _c(
        severe=None,
        sesoi=None,
        uses_sesoi=False,
        used_sesoi=False,
        effect=None,
        frozen=True,
    )
    assert claim_is_severe(bare) is False
    no_effect = _c(
        severe=None,
        sesoi=0.5,
        uses_sesoi=True,
        used_sesoi=True,
        effect=None,
        frozen=True,
    )
    assert claim_is_severe(no_effect) is False
    below = _c(
        severe=None,
        sesoi=0.5,
        uses_sesoi=True,
        used_sesoi=True,
        effect={"est": 0.1, "ci_low": 0.1, "ci_high": 0.1},
        predicted_direction="positive",
        frozen=True,
    )
    assert claim_is_severe(below) is False
    ok = _c(
        severe=None,
        sesoi=0.5,
        uses_sesoi=True,
        used_sesoi=True,
        effect={"est": 1.0, "ci_low": 1.0, "ci_high": 1.0},
        predicted_direction="positive",
        frozen=True,
    )
    assert claim_is_severe(ok) is True


def test_duplicate_runs_count_once():
    a = _c(intent="refute", independence_key="same", attempt_id="A1#1", experiment_id="A1")
    b = _c(
        intent="refute",
        independence_key="same",
        attempt_id="A1#2",
        experiment_id="A1",
        artifact_id="A2",
    )
    assert hypothesis_verdict([a, b], True, supported_needed=2)[0] == "survived"


def test_independence_key_base_is_experiment_id_no_method():
    """Key base is experiment_id; method is not part of the key."""
    k1 = independence_key_for(experiment_id="st/E1")
    k2 = independence_key_for(experiment_id="st/E2")
    assert k1 != k2
    assert k1 == independence_key_for(experiment_id="st/E1")
    # method kwargs rejected — signature is experiment_id only.
    import inspect

    assert list(inspect.signature(independence_key_for).parameters) == ["experiment_id"]


def test_data_digest_overlap_merges_sources():
    """Overlapping data digests → one source; method ignored."""
    a = _c(
        experiment_id="E1",
        independence_key=independence_key_for(experiment_id="E1"),
        data_digests=["D1", "D2"],
        attempt_id="E1#1",
    )
    b = _c(
        experiment_id="E2",
        independence_key=independence_key_for(experiment_id="E2"),
        data_digests=["D2", "D3"],
        attempt_id="E2#1",
        artifact_id="Art2",
    )
    c = _c(
        experiment_id="E3",
        independence_key=independence_key_for(experiment_id="E3"),
        data_digests=["D9"],
        attempt_id="E3#1",
        artifact_id="Art3",
    )
    assert len(distinct_by_key([a, b, c])) == 2


def test_no_input_identical_spec_one_source():
    from coscientist.core.domain import ExperimentMethod

    spec_a = TaskSpec(
        task_key="E1",
        title="t",
        goal="g",
        route="experiment",
        method=ExperimentMethod.COMPUTATIONAL,
        criteria=[RequirementRef(template_id="value_match")],
        addresses=["Q1"],
    )
    spec_b = TaskSpec(
        task_key="E2",
        title="t",
        goal="g",
        route="experiment",
        method=ExperimentMethod.COMPUTATIONAL,
        criteria=[RequirementRef(template_id="value_match")],
        addresses=["Q1"],
    )
    spec_c = TaskSpec(
        task_key="E3",
        title="other",
        goal="g",
        route="experiment",
        method=ExperimentMethod.COMPUTATIONAL,
        criteria=[RequirementRef(template_id="value_match")],
        addresses=["Q1"],
    )
    sd = normalized_task_spec_digest(spec_a)
    assert sd == normalized_task_spec_digest(spec_b)
    assert sd != normalized_task_spec_digest(spec_c)
    a = _c(
        experiment_id="E1",
        independence_key=independence_key_for(experiment_id="E1"),
        data_digests=[],
        spec_digest=sd,
        plan_id="P1",
        attempt_id="E1#1",
    )
    b = _c(
        experiment_id="E2",
        independence_key=independence_key_for(experiment_id="E2"),
        data_digests=[],
        spec_digest=sd,
        plan_id="P1",
        attempt_id="E2#1",
        artifact_id="Art2",
    )
    c = _c(
        experiment_id="E3",
        independence_key=independence_key_for(experiment_id="E3"),
        data_digests=[],
        spec_digest=normalized_task_spec_digest(spec_c),
        plan_id="P1",
        attempt_id="E3#1",
        artifact_id="Art3",
    )
    assert len(distinct_by_key([a, b])) == 1
    assert len(distinct_by_key([a, b, c])) == 2


def test_legacy_independence_key_uses_experiment_id():
    k = independence_key_for(experiment_id="st/E1")
    assert k == independence_key_for(experiment_id="st/E1")
    assert k != independence_key_for(experiment_id="st/E2")


def test_refuted_k_ref_2_needs_independent():
    r1 = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="r1",
        attempt_id="R1#1",
        sesoi=None,
        refute_capable=False,
    )
    r2 = _c(
        criterion_status="not_met",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="r2",
        attempt_id="R2#1",
        artifact_id="Art2",
        sesoi=None,
        refute_capable=False,
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
        sesoi=None,
        refute_capable=False,
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


def test_legacy_claims_never_supported():
    legacy = _c(severe=False, claim_outcome="consistent", intent="refute", sesoi=0.5)
    other = _c(
        severe=False,
        claim_outcome="consistent",
        intent="confirm",
        independence_key="b",
        attempt_id="B#1",
        experiment_id="B",
        artifact_id="B",
        sesoi=0.5,
    )
    assert (
        hypothesis_verdict([legacy, other], True, supported_needed=2)[0]
        == "inconclusive"
    )


def test_r17_violation_invalid():
    from coscientist.core.verdict import classify_outcome

    v, oc, _ = classify_outcome(
        validity_ok=True,
        prediction_statuses=["PASSED"],
        r17_ok=False,
    )
    assert v == "invalid" and oc is None


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
            "Q1": {"disposition": "survived", "reason": "", "formulation": "q"}
        },
        fallback_reason=None,
        hypotheses=hyps,
    )
    assert len(canonical_json(report)) <= MAX_BYTES


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
            supported_needed=self.k_sup,
        )

    @rule(
        validity=st.sampled_from(["valid", "invalid"]),
        outcome=st.sampled_from(
            ["consistent", "inconsistent", "indeterminate", None]
        ),
        provenance=st.sampled_from(["real", "partial", "stub"]),
        data=st.sampled_from(["D0", "D1", "D2"]),
        code=st.sampled_from(["C0", "C1"]),
        method=st.sampled_from(["computational", None]),
        lit=st.booleans(),
        intent=st.sampled_from(["confirm", "refute"]),
        with_threshold=st.booleans(),
        legacy=st.booleans(),
    )
    def record_claim(
        self,
        validity,
        outcome,
        provenance,
        data,
        code,
        method,
        lit,
        intent,
        with_threshold,
        legacy,
    ):
        self._n += 1
        if validity == "invalid":
            outcome = None
        if outcome is None and validity == "valid":
            outcome = "indeterminate"
        status = {
            "consistent": "met",
            "inconsistent": "not_met",
            "indeterminate": "unassessed",
            None: "unassessed",
        }[outcome]
        sesoi = 0.5 if with_threshold and not legacy else None
        # Do not hand-stamp severe=True — claim_is_severe derives from threshold.
        severe = False if legacy else None
        exp = f"A{self._n}"
        key = independence_key_for(experiment_id=exp)
        # method is ignored for independence; data digests drive merge.
        del method
        digests = data_digests_from_bindings(
            [
                {"kind": "artifact", "input": "data", "digest": data},
                {"kind": "artifact", "input": "code", "digest": code},
            ]
        )
        # Without artifact metas, both bindings classify as data — use data only.
        digests = [data]
        effect = (
            {"est": 1.5, "ci_low": 1.5, "ci_high": 1.5}
            if with_threshold and not legacy
            else None
        )
        c = _c(
            criterion_status=status,
            claim_outcome=outcome,
            validity_ok=(validity == "valid"),
            validity=validity,
            provenance=provenance,
            independence_key=key,
            attempt_id=f"{exp}#1",
            experiment_id=exp,
            artifact_id=f"Art{self._n}",
            evidence_kind="literature" if lit else "computational",
            lit_own_criterion=lit,
            intent=intent,
            severe=severe,
            sesoi=sesoi,
            uses_sesoi=bool(with_threshold and not legacy),
            used_sesoi=bool(with_threshold and not legacy),
            effect=effect,
            predicted_direction="positive",
            data_digests=digests,
            check_role="prediction",
            refute_capable=bool(
                with_threshold and not legacy and intent == "refute" and not lit
            ),
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
        """supported ⇒ ≥supported_needed distinct core keys, all severe, ≥1 capable refute."""
        v, _, _ = self._verdict()
        if v != "supported":
            return
        counted = [c for c in self.claims if c.counts()]
        severe = [
            c
            for c in counted
            if c.claim_outcome == "consistent" and claim_is_severe(c)
        ]
        assert severe and all(claim_is_severe(c) for c in severe)
        keys = {c.independence_key for c in severe}
        assert len(keys) >= self.k_sup
        assert any(refute_intent_counts(c) for c in severe)

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

    @invariant()
    def v5_legacy_never_supported(self):
        v, _, _ = self._verdict()
        if v != "supported":
            return
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


# --- Executor e2e (review blockers) -------------------------------------------


@pytest.mark.asyncio
async def test_e2e_no_sesoi_never_supported(tmp_path, clock, runtime_cls):
    """Catalog without uses_sesoi → not severe → survived at most, never supported."""
    from coscientist.core.artifacts import make_tool_ref
    from coscientist.core.dispositions import dispositions

    sid = "st_nose"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid,
        finalize_reserve_s=5,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="producer",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                # value_match has no uses_sesoi in catalog.
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid0 = await rt.start_attempt("E0")
    art = arts.put(
        b'{"count": 225}',
        make_tool_ref(f"{aid0}:1"),
        name="main",
        media_type="application/json",
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 225},
            claims=[{"id": "c1", "target": "Q1"}],
        ),
    )
    by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
    claim = by["question:Q1"][0]
    assert claim.severe is False
    assert claim.sesoi is None
    d = dispositions(
        tasks=rt.tasks, plans=rt.plans, graph=rt.graph, study=rt.study, results=rt.results
    )
    assert d["Q1"]["verdict"] != "supported"
    assert d["Q1"]["verdict"] == "inconclusive"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_e2e_independence_sources(tmp_path, clock, runtime_cls):
    """Data-overlap merge; no-input identical specs; method ignored."""
    from coscientist.core.artifacts import make_tool_ref
    from coscientist.core.domain import ExperimentMethod

    sid = "st_indep"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")

    await rt.accept_plan(
        [
            TaskSpec(
                task_key="P_DATA",
                title="data",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                expected_outputs=[
                    OutputSpec(name="data_a", media_type="application/json", role="data"),
                    OutputSpec(name="data_b", media_type="application/json", role="data"),
                    OutputSpec(
                        name="code", media_type="text/x-python", role="code"
                    ),
                ],
                criteria=[RequirementRef(template_id="artifact_present")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E1",
                title="exp1",
                goal="g",
                route="experiment",
                depends_on=["P_DATA"],
                method=ExperimentMethod.COMPUTATIONAL,
                inputs=[
                    InputSpec(from_task="P_DATA", output="data_a"),
                    InputSpec(from_task="P_DATA", output="code"),
                ],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E2",
                title="exp2",
                goal="g",
                route="experiment",
                depends_on=["P_DATA"],
                method=ExperimentMethod.STATISTICAL,  # different method, same data as E3
                inputs=[
                    InputSpec(from_task="P_DATA", output="data_b"),
                    InputSpec(from_task="P_DATA", output="code"),
                ],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E3",
                title="exp3_reuse",
                goal="g",
                route="experiment",
                depends_on=["P_DATA"],
                method=ExperimentMethod.SIMULATION,  # different method, same data as E1
                inputs=[
                    InputSpec(from_task="P_DATA", output="data_a"),
                    InputSpec(from_task="P_DATA", output="code"),
                ],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
            # No-input: identical specs (different task_key) → 1 source.
            TaskSpec(
                task_key="N1",
                title="same",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                method=ExperimentMethod.COMPUTATIONAL,
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="N2",
                title="same",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                method=ExperimentMethod.COMPUTATIONAL,
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
            # No-input: different spec → 2nd source.
            TaskSpec(
                task_key="N3",
                title="different",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                method=ExperimentMethod.COMPUTATIONAL,
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid_p = await rt.start_attempt("P_DATA")
    da = arts.put(
        b'{"count": 1}',
        make_tool_ref(f"{aid_p}:da"),
        name="data_a",
        media_type="application/json",
        role="data",
    )
    db = arts.put(
        b'{"count": 2}',
        make_tool_ref(f"{aid_p}:db"),
        name="data_b",
        media_type="application/json",
        role="data",
    )
    code = arts.put(
        b"print(1)",
        make_tool_ref(f"{aid_p}:c"),
        name="code",
        media_type="text/x-python",
        role="code",
    )
    await rt.executor.record(
        aid_p,
        AgentTaskOutput(
            summary="data",
            outputs={
                "main": ArtifactHandle(artifact_id=da.artifact_id, name="main"),
                "data_a": ArtifactHandle(artifact_id=da.artifact_id, name="data_a"),
                "data_b": ArtifactHandle(artifact_id=db.artifact_id, name="data_b"),
                "code": ArtifactHandle(artifact_id=code.artifact_id, name="code"),
            },
            claims=[],
        ),
    )

    claims_by_tk: dict[str, object] = {}
    for tk in ("E1", "E2", "E3", "N1", "N2", "N3"):
        aid = await rt.start_attempt(tk)
        art = arts.put(
            b'{"count": 225}',
            make_tool_ref(f"{aid}:1"),
            name="main",
            media_type="application/json",
            role="data",
        )
        await rt.executor.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
                values={"count": 225},
                claims=[{"id": f"c_{tk}", "target": "Q1"}],
            ),
        )
        by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
        for c in by["question:Q1"]:
            if c.attempt_id == aid:
                claims_by_tk[tk] = c
                break

    e1, e2, e3 = claims_by_tk["E1"], claims_by_tk["E2"], claims_by_tk["E3"]
    # Stamped keys are experiment-based (distinct), merge is at count time.
    assert e1.independence_key != e2.independence_key
    assert e1.independence_key != e3.independence_key
    # Different methods over same data → 1 source; different data → 2.
    assert len(distinct_by_key([e1, e3])) == 1
    assert len(distinct_by_key([e1, e2, e3])) == 2
    # Code role ignored: data digests only.
    assert da.sha256 in (e1.data_digests or [])
    assert code.sha256 not in (e1.data_digests or [])

    n1, n2, n3 = claims_by_tk["N1"], claims_by_tk["N2"], claims_by_tk["N3"]
    assert n1.spec_digest and n1.spec_digest == n2.spec_digest
    assert n1.spec_digest != n3.spec_digest
    assert len(distinct_by_key([n1, n2])) == 1
    assert len(distinct_by_key([n1, n2, n3])) == 2
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_e2e_one_severe_defaults_survived(tmp_path, clock, runtime_cls):
    """supported_needed default 2: one severe claim → survived."""
    from coscientist.core.artifacts import make_tool_ref
    from coscientist.core.dispositions import dispositions

    sid = "st_one"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid,
        finalize_reserve_s=5,
    )
    assert rt.study.supported_needed == 2
    assert rt.study.confirmations_needed == 1
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="producer",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match_sesoi")],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid0 = await rt.start_attempt("E0")
    # effect 2.0 > sesoi 0.5 → severe
    art = arts.put(
        b'{"effect": 2.0}',
        make_tool_ref(f"{aid0}:1"),
        name="main",
        media_type="application/json",
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"effect": 2.0},
            claims=[{"id": "c1", "target": "Q1"}],
        ),
    )
    by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
    assert by["question:Q1"][0].severe is True
    d = dispositions(
        tasks=rt.tasks, plans=rt.plans, graph=rt.graph, study=rt.study, results=rt.results
    )
    assert d["Q1"]["verdict"] == "survived"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_verdict_replay_equals_finalized_snapshots(tmp_path, clock, runtime_cls):
    """V1: journaled VerdictSnapshotsRecorded wins on reopen (not recomputed softer)."""
    from coscientist.core.artifacts import make_tool_ref

    sid = "st_v1"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid,
        finalize_reserve_s=5,
        confirmations_needed=1,
        supported_needed=2,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="producer",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match_sesoi")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E1",
                title="consumer",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                inputs=[InputSpec(kind="verdict", ref="Q1")],
                criteria=[RequirementRef(template_id="artifact_present")],
                addresses=["Q1"],
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid0 = await rt.start_attempt("E0")
    art = arts.put(
        b'{"effect": 2.0}',
        make_tool_ref(f"{aid0}:1"),
        name="main",
        media_type="application/json",
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"effect": 2.0},
            claims=[{"id": "c1", "target": "Q1"}],
        ),
    )
    # Bind E1 → records VerdictSnapshotsRecorded for Q1.
    await rt.start_attempt("E1")
    snap1 = dict(rt.verdict_snapshots.get("Q1") or {})
    assert snap1.get("verdict") == "survived"
    await close_runtime(rt)

    # Reopen: journaled snapshot preferred.
    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    assert rt2.verdict_snapshots.get("Q1", {}).get("verdict") == snap1.get("verdict")
    assert rt2.study.supported_needed == 2
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_aggregate_evalue_rejected_at_create(tmp_path, clock, runtime_cls):
    sid = "st_ev"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    with pytest.raises(ValueError, match="evalue"):
        await rt.create_study(
            profile="research",
            request="q",
            catalog_version="0.1.0",
            deadline=clock.now() + timedelta(seconds=60),
            sid=sid,
            finalize_reserve_s=5,
            aggregate="evalue",
        )
    await close_runtime(rt)
