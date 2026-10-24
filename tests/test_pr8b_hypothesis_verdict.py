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

from coscientist.core.claims.claims import RuntimeClaim, filtered_claims_by_target
from coscientist.core.claims.rules import verdict
from coscientist.core.claims.verdict import (
    claim_is_severe,
    data_digests_from_bindings,
    distinct_by_key,
    exclusion_reason,
    hypothesis_report_row,
    hypothesis_verdict,
    independence_key_for,
    refute_intent_counts,
    root_data_digests_from_bindings,
    tost_holds,
)
from coscientist.core.events.upcasters import upcast
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    InputSpec,
    OutputSpec,
    QuestionSpec,
    RequirementRef,
    TaskSpec,
)
from tests.helpers import attach_executor, close_runtime, open_runtime


def _c(**kw) -> RuntimeClaim:
    """Severe-capable claim helper (catalog sesoi + effect exceeding it)."""
    base = dict(
        criterion_status="met",
        check_role="prediction",
        target_kind="hypothesis",
        accepted=True,
        provenance="real",
        frozen=True,
        evidence_kind="computational",
        attempt_id="A1#1",
        artifact_id="Art1",
        validity_ok=True,
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
        # Unit tests stamp countable core-evaluated evidence (production path).
        effect={
            "est": 1.5,
            "ci_low": 1.5,
            "ci_high": 1.5,
            "producer": "agent",
            "trusted": False,
            "core_evaluated": True,
            "countable": True,
            "produced_by_call_id": "A1#1:1",
        },
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
    v, _, _, rule = hypothesis_verdict(
        [stub, real_ref],
        True,
        refutations_needed=1,
        refutation_priority=True,
    )
    assert v == "refuted" and rule == "k_ref"
    v2, _, _, _ = hypothesis_verdict(
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
    from coscientist.core.claims.verdict import excluded_confirmations_note

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
    assert v == 12
    assert out["supported_needed"] == 2
    assert out.get("aggregate", "count") == "count"
    assert out.get("user_input_ids") == []
    assert out["max_expansions"] == 2
    assert out["max_proposals"] == 3


def test_upcaster_studycreated_v4_to_v5_defaults():
    v, out = upcast("StudyCreated", 4, {"study_id": "s", "confirmations_needed": 1})
    assert v == 12
    assert out["aggregate"] == "count"
    assert out["supported_needed"] == 2
    assert out.get("as_of") is None
    assert "min_independent_support" not in out
    assert out["replan_reserve"] == 0.1
    assert out["max_proposals"] == 3


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
    from coscientist.core.claims.verdict import (
        classify_outcome,
        tost_holds,
        tost_holds_numerically,
    )

    effect = {"ci_low": -0.1, "ci_high": 0.1, "est": 0.0, "countable": False}
    assert not tost_holds_numerically(sesoi=None, effect=effect)
    assert tost_holds_numerically(sesoi=1.0, effect=effect)
    # Severe-grade TOST requires countable core-evaluated evidence.
    assert not tost_holds(sesoi=1.0, effect=effect)
    assert tost_holds(
        sesoi=1.0, effect={**effect, "countable": True, "core_evaluated": True}
    )
    v, oc, _ = classify_outcome(
        validity_ok=True,
        prediction_statuses=["PASSED"],
        predicts_null=True,
        sesoi=None,
        effect=effect,
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
    """Severe needs uses_sesoi + used_sesoi + countable effect exceeding sesoi."""
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
    not_countable = _c(
        severe=None,
        sesoi=0.5,
        uses_sesoi=True,
        used_sesoi=True,
        effect={
            "est": 999.0,
            "kind": "point",
            "core_evaluated": False,
            "countable": False,
        },
        predicted_direction="positive",
        frozen=True,
    )
    assert claim_is_severe(not_countable) is False
    below = _c(
        severe=None,
        sesoi=0.5,
        uses_sesoi=True,
        used_sesoi=True,
        effect={
            "est": 0.1,
            "ci_low": 0.1,
            "ci_high": 0.1,
            "core_evaluated": True,
            "countable": True,
        },
        predicted_direction="positive",
        frozen=True,
    )
    assert claim_is_severe(below) is False
    ok = _c(
        severe=None,
        sesoi=0.5,
        uses_sesoi=True,
        used_sesoi=True,
        effect={
            "est": 1.0,
            "ci_low": 1.0,
            "ci_high": 1.0,
            "core_evaluated": True,
            "countable": True,
        },
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


def test_structural_shared_template_merges():
    """Same target + any shared template + empty roots → one source."""
    a = _c(
        experiment_id="E1",
        independence_key=independence_key_for(experiment_id="E1"),
        data_digests=[],
        target="H1",
        criterion_template_ids=["value_match"],
        attempt_id="E1#1",
    )
    b = _c(
        experiment_id="E2",
        independence_key=independence_key_for(experiment_id="E2"),
        data_digests=[],
        target="H1",
        # Extra template must not mint a new source (share-any).
        criterion_template_ids=["value_match", "artifact_present"],
        attempt_id="E2#1",
        artifact_id="Art2",
    )
    c = _c(
        experiment_id="E3",
        independence_key=independence_key_for(experiment_id="E3"),
        data_digests=[],
        target="H1",
        criterion_template_ids=["tost_equivalence"],
        attempt_id="E3#1",
        artifact_id="Art3",
    )
    assert len(distinct_by_key([a, b])) == 1
    assert len(distinct_by_key([a, b, c])) == 2


def test_goal_reword_does_not_split_structural_source():
    """Goal/title/plan_id are not part of the structural key."""
    a = _c(
        experiment_id="E1",
        data_digests=[],
        target="H1",
        criterion_template_ids=["value_match_sesoi"],
        attempt_id="E1#1",
    )
    b = _c(
        experiment_id="E2",
        data_digests=[],
        target="H1",
        criterion_template_ids=["value_match_sesoi"],
        attempt_id="E2#1",
        artifact_id="Art2",
    )
    assert len(distinct_by_key([a, b])) == 1


def test_root_data_walk_through_intermediate():
    """Dataset via intermediate producer merges with direct raw binding."""
    raw_sha = "aa" * 32
    mid_id = "art_mid"
    raw_id = "art_raw"

    class _Meta:
        def __init__(self, sha: str, role: str = "data") -> None:
            self.sha256 = sha
            self.role = role
            self.media_type = "application/json"

    def lookup(aid: str):
        if aid == raw_id:
            return _Meta(raw_sha)
        if aid == mid_id:
            return _Meta("bb" * 32)
        return None

    results = {
        "MID#1": {
            "artifacts": [mid_id],
            "output_map": {"mid": mid_id},
            "inputs": [
                {"kind": "artifact", "input": "data", "artifact_id": raw_id},
            ],
        }
    }
    via_mid, _n = root_data_digests_from_bindings(
        [{"kind": "artifact", "input": "mid", "artifact_id": mid_id}],
        results=results,
        artifact_lookup=lookup,
    )
    direct, _n2 = root_data_digests_from_bindings(
        [{"kind": "artifact", "input": "data", "artifact_id": raw_id}],
        results=results,
        artifact_lookup=lookup,
    )
    assert via_mid == [raw_sha]
    assert direct == [raw_sha]


def test_slot_rename_cannot_hide_data_roots():
    """Binding slot name (e.g. 'script') must not classify data as code."""
    sha = "cc" * 32

    class _Meta:
        sha256 = sha
        role = "data"
        media_type = "application/json"

    digs = data_digests_from_bindings(
        [{"kind": "artifact", "input": "script", "artifact_id": "art1"}],
        artifact_lookup=lambda _a: _Meta(),
        code_output_names=["code", "script", "program"],
    )
    assert digs == [sha]


def test_output_kinds_from_catalog_ignores_planner_role():
    """MJ2: kinds from catalog code_output_names; planner role ignored; else data."""
    from coscientist.core.claims.verdict import output_kinds_from_catalog

    catalog = type("C", (), {"code_output_names": ["script", "program"]})()
    kinds = output_kinds_from_catalog(
        output_names=["dataset", "script", "other"],
        catalog=catalog,
    )
    assert kinds == {"dataset": "data", "script": "code", "other": "data"}
    # Empty catalog list → everything data (planner cannot override).
    empty = type("C", (), {"code_output_names": []})()
    assert output_kinds_from_catalog(
        output_names=["dataset"], catalog=empty
    ) == {"dataset": "data"}


def test_artifact_role_code_skipped():
    """Core-recorded Artifact.role=code is skipped (not a data root)."""
    sha = "dd" * 32

    class _Meta:
        sha256 = sha
        role = "code"
        media_type = "text/x-python"

    digs = data_digests_from_bindings(
        [{"kind": "artifact", "input": "anything", "artifact_id": "art1"}],
        artifact_lookup=lambda _a: _Meta(),
    )
    assert digs == []


def test_all_code_upstream_intermediate_is_root():
    """Generation/simulation whose inputs are all code keeps the intermediate as root."""
    code_sha = "ee" * 32
    mid_sha = "ff" * 32
    code_id = "art_code"
    mid_id = "art_mid"

    class _Meta:
        def __init__(self, sha: str, role: str) -> None:
            self.sha256 = sha
            self.role = role
            self.media_type = (
                "text/x-python" if role == "code" else "application/json"
            )

    def lookup(aid: str):
        if aid == code_id:
            return _Meta(code_sha, "code")
        if aid == mid_id:
            return _Meta(mid_sha, "data")
        return None

    results = {
        "GEN#1": {
            "artifacts": [mid_id],
            "output_map": {"out": mid_id},
            "output_kinds": {"out": "data"},
            "inputs": [
                {"kind": "artifact", "input": "script", "artifact_id": code_id},
            ],
        },
        "CODE#1": {
            "artifacts": [code_id],
            "output_map": {"script": code_id},
            "output_kinds": {"script": "code"},
            "inputs": [],
        },
    }
    digs, notes = root_data_digests_from_bindings(
        [{"kind": "artifact", "input": "out", "artifact_id": mid_id}],
        results=results,
        artifact_lookup=lookup,
    )
    assert digs == [mid_sha]
    assert code_sha not in digs
    assert isinstance(notes, list)


def test_diamond_provenance_not_flagged_as_cycle():
    """Shared ancestor (diamond) must not emit cycle: notes."""
    root_sha = "11" * 32
    a_sha, b_sha, top_sha = "22" * 32, "33" * 32, "44" * 32
    root_id, a_id, b_id, top_id = "art_r", "art_a", "art_b", "art_t"

    class _Meta:
        def __init__(self, sha: str) -> None:
            self.sha256 = sha
            self.role = "data"
            self.media_type = "application/json"

    metas = {
        root_id: _Meta(root_sha),
        a_id: _Meta(a_sha),
        b_id: _Meta(b_sha),
        top_id: _Meta(top_sha),
    }
    results = {
        "A#1": {
            "artifacts": [a_id],
            "output_map": {"a": a_id},
            "output_kinds": {"a": "data"},
            "inputs": [{"kind": "artifact", "input": "r", "artifact_id": root_id}],
        },
        "B#1": {
            "artifacts": [b_id],
            "output_map": {"b": b_id},
            "output_kinds": {"b": "data"},
            "inputs": [{"kind": "artifact", "input": "r", "artifact_id": root_id}],
        },
        "T#1": {
            "artifacts": [top_id],
            "output_map": {"t": top_id},
            "output_kinds": {"t": "data"},
            "inputs": [
                {"kind": "artifact", "input": "a", "artifact_id": a_id},
                {"kind": "artifact", "input": "b", "artifact_id": b_id},
            ],
        },
    }
    digs, notes = root_data_digests_from_bindings(
        [{"kind": "artifact", "input": "t", "artifact_id": top_id}],
        results=results,
        artifact_lookup=lambda a: metas.get(a),
    )
    assert digs == [root_sha]
    assert not any(n.startswith("cycle:") for n in notes)


def test_agent_media_type_spoof_does_not_drop_data_root():
    """Re-saving a dataset as text/x-python must not hide it when OutputSpec is data."""
    sha = "55" * 32
    aid = "art_spoof"

    class _Meta:
        sha256 = sha
        role = "code"  # agent put inferred code from media_type
        media_type = "text/x-python"
        producer = "agent"

    results = {
        "P#1": {
            "artifacts": [aid],
            "output_map": {"dataset": aid},
            "output_kinds": {"dataset": "data"},  # core OutputSpec wins
            "inputs": [],
        }
    }
    digs, _n = root_data_digests_from_bindings(
        [{"kind": "artifact", "input": "dataset", "artifact_id": aid}],
        results=results,
        artifact_lookup=lambda _a: _Meta(),
    )
    assert digs == [sha]


@pytest.mark.asyncio
async def test_e2e_media_type_spoof_keeps_data_root(tmp_path, clock, runtime_cls):
    """Agent put with text/x-python on a data OutputSpec stays in root digests."""
    from coscientist.core.execution.artifacts import make_tool_ref

    sid = "st_spoof"
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
                task_key="P0",
                title="raw",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                expected_outputs=[
                    # Planner declares role=code; catalog does not list "dataset".
                    OutputSpec(
                        name="dataset", media_type="application/json", role="code"
                    )
                ],
                criteria=[RequirementRef(template_id="artifact_present")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E0",
                title="use",
                goal="g",
                route="experiment",
                depends_on=["P0"],
                inputs=[InputSpec(from_task="P0", output="dataset")],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="why", final_type="verdict")],
    )
    aid_p = await rt.start_attempt("P0")
    # Spoof: agent labels bytes as python; planner role=code — catalog still data.
    raw = arts.put(
        b'{"count": 225}',
        make_tool_ref(f"{aid_p}:1"),
        name="dataset",
        media_type="text/x-python",
        role="code",
    )
    assert raw.role == "code"  # explicit role (no media_type heuristic)
    await rt.executor.record(
        aid_p,
        AgentTaskOutput(
            summary="raw",
            outputs={
                "main": ArtifactHandle(artifact_id=raw.artifact_id, name="main"),
                "dataset": ArtifactHandle(artifact_id=raw.artifact_id, name="dataset"),
            },
            claims=[],
        ),
    )
    assert rt.results[aid_p].get("output_kinds", {}).get("dataset") == "data"
    aid_e = await rt.start_attempt("E0")
    art = arts.put(
        b'{"count": 225}',
        make_tool_ref(f"{aid_e}:1"),
        name="main",
        media_type="application/json",
        role="data",
    )
    await rt.executor.record(
        aid_e,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 225},
            claims=[{"id": "c1", "target": "Q1", "target_kind": "question", "checked_against": "value_match"}],
        ),
    )
    by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
    c = by["question:Q1"][0]
    assert raw.sha256 in (c.data_digests or [])
    await close_runtime(rt)


def test_non_countable_fail_is_indeterminate_not_refutation():
    from coscientist.core.claims.verdict import classify_outcome

    v, oc, _ = classify_outcome(
        validity_ok=True,
        prediction_statuses=["FAILED"],
        effect={
            "est": 0.0,
            "kind": "point",
            "producer": "agent",
            "core_evaluated": False,
            "countable": False,
        },
    )
    assert v == "valid" and oc == "indeterminate"
    v2, oc2, _ = classify_outcome(
        validity_ok=True,
        prediction_statuses=["FAILED"],
        effect={
            "est": 0.0,
            "kind": "point",
            "producer": "agent",
            "core_evaluated": True,
            "countable": True,
        },
    )
    assert v2 == "valid" and oc2 == "inconsistent"
    non_count = _c(
        criterion_status="unassessed",
        claim_outcome="indeterminate",
        severe=False,
        effect={
            "est": 0.0,
            "kind": "point",
            "producer": "agent",
            "core_evaluated": False,
            "countable": False,
        },
        exclusion_reason="indeterminate",
    )
    assert hypothesis_verdict([non_count], True)[0] == "inconclusive"


def test_tost_requires_real_ci_and_countable():
    """Point estimate / non-countable CI is not TOST-severe."""
    sesoi = 0.5
    point = {
        "est": 0.0,
        "ci_low": 0.0,
        "ci_high": 0.0,
        "kind": "point",
        "countable": False,
    }
    assert tost_holds(sesoi=sesoi, effect=point) is False
    no_count = {
        "est": 0.0,
        "ci_low": -0.1,
        "ci_high": 0.1,
        "kind": "interval",
        "countable": False,
        "core_evaluated": False,
    }
    assert tost_holds(sesoi=sesoi, effect=no_count) is False
    countable = {**no_count, "countable": True, "core_evaluated": True}
    assert tost_holds(sesoi=sesoi, effect=countable) is True
    c = _c(
        predicts_null=True,
        used_sesoi=True,
        uses_sesoi=True,
        sesoi=sesoi,
        effect=no_count,
        severe=None,
    )
    assert claim_is_severe(c) is False
    assert refute_intent_counts(
        _c(
            intent="refute",
            predicts_null=True,
            used_sesoi=True,
            uses_sesoi=True,
            sesoi=sesoi,
            effect=no_count,
            refute_capable=True,  # forged True must not win
        )
    ) is False


def test_value_matches_never_used_sesoi():
    import tempfile
    from pathlib import Path

    from coscientist.checks.evaluate import evaluate_code_check
    from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
    from coscientist.core.execution.domain import CodeCheck

    with tempfile.TemporaryDirectory() as tmp:
        arts = LocalArtifactStore(Path(tmp))
        art = arts.put(
            b'{"count": 225}',
            make_tool_ref("t:1"),
            name="main",
            media_type="application/json",
            role="data",
        )
        cr = evaluate_code_check(
            CodeCheck(kind="value_matches", output="main", pointer="/count", target=225),
            template_id="value_match",
            check_id="value_match#0",
            out=AgentTaskOutput(
                summary="ok",
                outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
                values={"count": 225},
            ),
            artifacts=arts,
            values={"count": 225},
            role="prediction",
            uses_sesoi=True,
            sesoi=0.5,
            predicts_null=False,
        )
        assert cr.used_sesoi is False


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
    v, _, _, rule = hypothesis_verdict(
        [r1], True, refutations_needed=2, refutation_priority=True
    )
    assert v == "inconclusive" and rule == "inc_below_k_ref"


def test_disposition_mapping_with_hypotheses():
    from coscientist.core.claims.verdict import disposition_for_question

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
    from coscientist.core.claims.verdict import classify_outcome

    v, oc, _ = classify_outcome(
        validity_ok=True,
        prediction_statuses=["PASSED"],
        r17_ok=False,
    )
    assert v == "invalid" and oc is None


def test_minimal_report_hypotheses_truncation_le_10kb():
    from coscientist.core.events.digest import canonical_json
    from coscientist.core.execution.domain import Task, TaskKind, TaskOutcome, TaskSpec, TaskState
    from coscientist.core.finalize.minimal_report import MAX_BYTES, build_minimal_report

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
        self._last: tuple[str, bool, bool, str | None] | None = None

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
            {
                "est": 1.5,
                "ci_low": 1.5,
                "ci_high": 1.5,
                "producer": "agent",
                "trusted": False,
                "core_evaluated": True,
                "countable": True,
                "produced_by_call_id": f"{exp}#1:1",
            }
            if (with_threshold and not legacy) or outcome == "inconsistent"
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
        v, _, _, _ = self._verdict()
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
        v, _, _, _ = self._verdict()
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
        v, _, _, _ = self._verdict()
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
    from coscientist.core.claims.dispositions import dispositions
    from coscientist.core.execution.artifacts import make_tool_ref

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
        questions=[QuestionSpec(id="Q1", text="why", final_type="verdict")],
    )
    aid0 = await rt.start_attempt("E0")
    art = arts.put(
        b'{"count": 225}',
        make_tool_ref(f"{aid0}:1"),
        name="main",
        media_type="application/json",
        role="data",
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 225},
            claims=[{"id": "c1", "target": "Q1", "target_kind": "question", "checked_against": "value_match"}],
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
async def test_e2e_independence_bypasses(tmp_path, clock, runtime_cls):
    """Role swap, goal reword, and intermediate-step provenance cannot split sources."""
    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.domain import ExperimentMethod

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
                task_key="P_RAW",
                title="raw",
                goal="produce raw dataset",
                route="experiment",
                depends_on=["PLAN"],
                expected_outputs=[
                    # Planner may lie with role=code; catalog decides kinds.
                    OutputSpec(
                        name="dataset", media_type="application/json", role="code"
                    ),
                    OutputSpec(
                        name="data_b", media_type="application/json", role="data"
                    ),
                    OutputSpec(
                        name="code", media_type="text/x-python", role="code"
                    ),
                ],
                criteria=[RequirementRef(template_id="artifact_present")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="P_MID",
                title="intermediate",
                goal="pass-through",
                route="experiment",
                depends_on=["P_RAW"],
                inputs=[InputSpec(from_task="P_RAW", output="dataset")],
                expected_outputs=[
                    OutputSpec(name="mid", media_type="application/json", role="data"),
                ],
                criteria=[RequirementRef(template_id="artifact_present")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E1",
                title="via mid",
                goal="goal wording A",
                route="experiment",
                depends_on=["P_MID"],
                method=ExperimentMethod.COMPUTATIONAL,
                inputs=[
                    InputSpec(from_task="P_MID", output="mid"),
                    InputSpec(from_task="P_RAW", output="code"),
                ],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E2",
                title="direct raw",
                goal="goal wording B — revised plan",
                route="experiment",
                depends_on=["P_RAW"],
                method=ExperimentMethod.STATISTICAL,
                inputs=[
                    # Slot rename: bind data output under input name "script".
                    InputSpec(from_task="P_RAW", output="dataset"),
                    InputSpec(from_task="P_RAW", output="code"),
                ],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E3",
                title="other data",
                goal="g",
                route="experiment",
                depends_on=["P_RAW"],
                method=ExperimentMethod.SIMULATION,
                inputs=[
                    InputSpec(from_task="P_RAW", output="data_b"),
                    InputSpec(from_task="P_RAW", output="code"),
                ],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
            # No-input: goal/title reword + same templates → 1 source.
            TaskSpec(
                task_key="N1",
                title="same-ish",
                goal="original goal text",
                route="experiment",
                depends_on=["PLAN"],
                method=ExperimentMethod.COMPUTATIONAL,
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="N2",
                title="reworded title",
                goal="completely different goal after plan revision",
                route="experiment",
                depends_on=["PLAN"],
                method=ExperimentMethod.COMPUTATIONAL,
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
            # Distinct criterion template → separate structural source.
            TaskSpec(
                task_key="N3",
                title="other criterion",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                method=ExperimentMethod.COMPUTATIONAL,
                criteria=[RequirementRef(template_id="artifact_present")],
                addresses=["Q1"],
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="why", final_type="verdict")],
    )
    aid_p = await rt.start_attempt("P_RAW")
    da = arts.put(
        b'{"count": 1}',
        make_tool_ref(f"{aid_p}:da"),
        name="dataset",
        media_type="application/json",
        role="data",  # role required (no media_type heuristic)
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
    assert da.role == "data"
    await rt.executor.record(
        aid_p,
        AgentTaskOutput(
            summary="data",
            outputs={
                "main": ArtifactHandle(artifact_id=da.artifact_id, name="main"),
                "dataset": ArtifactHandle(artifact_id=da.artifact_id, name="dataset"),
                "data_b": ArtifactHandle(artifact_id=db.artifact_id, name="data_b"),
                "code": ArtifactHandle(artifact_id=code.artifact_id, name="code"),
            },
            claims=[],
        ),
    )
    assert rt.results[aid_p].get("output_kinds", {}).get("dataset") == "data"
    assert rt.results[aid_p].get("output_kinds", {}).get("code") == "code"
    aid_mid = await rt.start_attempt("P_MID")
    mid = arts.put(
        b'{"count": 1, "passed": true}',
        make_tool_ref(f"{aid_mid}:m"),
        name="mid",
        media_type="application/json",
        role="data",
    )
    await rt.executor.record(
        aid_mid,
        AgentTaskOutput(
            summary="mid",
            outputs={
                "main": ArtifactHandle(artifact_id=mid.artifact_id, name="main"),
                "mid": ArtifactHandle(artifact_id=mid.artifact_id, name="mid"),
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
        against = "artifact_present" if tk == "N3" else "value_match"
        await rt.executor.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
                values={"count": 225},
                claims=[{"id": f"c_{tk}", "target": "Q1", "target_kind": "question", "checked_against": against}],
            ),
        )
        by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
        for c in by["question:Q1"]:
            if c.attempt_id == aid:
                claims_by_tk[tk] = c
                break

    e1, e2, e3 = claims_by_tk["E1"], claims_by_tk["E2"], claims_by_tk["E3"]
    assert e1.independence_key != e2.independence_key
    # Intermediate + direct raw share root data_a → one source (bypass blocked).
    assert da.sha256 in (e1.data_digests or [])
    assert da.sha256 in (e2.data_digests or [])
    assert len(distinct_by_key([e1, e2])) == 1
    assert len(distinct_by_key([e1, e2, e3])) == 2
    # Slot rename to "script" did not hide the JSON data root; real code skipped.
    assert code.sha256 not in (e1.data_digests or [])

    n1, n2, n3 = claims_by_tk["N1"], claims_by_tk["N2"], claims_by_tk["N3"]
    # Share-any: N1/N2 same templates; N3 different family → separate source.
    assert len(distinct_by_key([n1, n2])) == 1
    assert len(distinct_by_key([n1, n2, n3])) == 2
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_e2e_agent_put_core_eval_refutes(tmp_path, clock, runtime_cls):
    """Production path: agent put + core catalog eval → countable not_met refutes."""
    from coscientist.core.claims.dispositions import dispositions
    from coscientist.core.execution.artifacts import make_tool_ref

    sid = "st_agent_ref"
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
                title="falsify",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match", required=True)],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why", final_type="verdict")],
    )
    aid0 = await rt.start_attempt("E0", provenance="real")
    art = arts.put(
        b'{"count": 999}',
        make_tool_ref(f"{aid0}:1"),
        name="main",
        media_type="application/json",
        role="data",
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="falsified",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 999},
            claims=[{"id": "ref", "target": "Q1", "target_kind": "question", "checked_against": "value_match"}],
        ),
    )
    by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
    c = by["question:Q1"][0]
    assert (c.effect or {}).get("countable") is True
    assert (c.effect or {}).get("trusted") is False
    assert c.claim_outcome == "inconsistent"
    d = dispositions(
        tasks=rt.tasks, plans=rt.plans, graph=rt.graph, study=rt.study, results=rt.results
    )
    assert d["Q1"]["verdict"] == "refuted"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_e2e_foreign_artifact_finalize_reason(tmp_path, clock, runtime_cls):
    """record→finalize: foreign_artifact → StudyFinalized reason, not unclear."""
    from coscientist.core.execution.artifacts import make_tool_ref

    sid = "st_foreign"
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
                title="t",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why", final_type="verdict")],
    )
    aid = await rt.start_attempt("E0")
    foreign = arts.put(
        b'{"count": 225}',
        make_tool_ref("other_attempt#1:1"),
        name="main",
        media_type="application/json",
        role="data",
    )
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="x",
            outputs={
                "main": ArtifactHandle(artifact_id=foreign.artifact_id, name="main")
            },
            values={"count": 225},
            claims=[{"id": "c1", "target": "Q1", "target_kind": "question", "checked_against": "value_match"}],
        ),
    )
    by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
    c = by["question:Q1"][0]
    assert c.claim_outcome == "indeterminate"
    assert c.exclusion_reason == "foreign_artifact"
    assert c.effect is None
    await rt.start_attempt("REPORT")
    await rt.cancel_task("REPORT", reason="cancelled")
    await rt.tick()
    assert rt._finalized
    events = await rt.store.read_all()
    fin = next(e for e in events if e.type == "StudyFinalized")
    q = fin.payload["question_dispositions"]["Q1"]
    assert q["disposition"] == "inconclusive"
    assert q["reason"] == "foreign_artifact"
    assert q["reason"] != "unclear"
    assert any(
        e.get("reason") == "foreign_artifact" for e in (q.get("excluded_claims") or [])
    )
    assert fin.payload.get("report_artifact_id") or fin.payload.get("minimal_report")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_e2e_missing_output_finalize_reason(tmp_path, clock, runtime_cls):
    """record→finalize: no output submitted → output_not_submitted on StudyFinalized."""
    sid = "st_no_out"
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
    attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why", final_type="verdict")],
    )
    aid = await rt.start_attempt("E0")
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="no artifact",
            outputs={},
            values={},
            claims=[{"id": "c1", "target": "Q1", "target_kind": "question", "checked_against": "value_match"}],
        ),
    )
    by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
    c = by["question:Q1"][0]
    assert c.claim_outcome == "indeterminate"
    assert c.exclusion_reason == "output_not_submitted"
    await rt.start_attempt("REPORT")
    await rt.cancel_task("REPORT", reason="cancelled")
    await rt.tick()
    assert rt._finalized
    events = await rt.store.read_all()
    fin = next(e for e in events if e.type == "StudyFinalized")
    q = fin.payload["question_dispositions"]["Q1"]
    assert q["disposition"] == "inconclusive"
    assert q["reason"] == "output_not_submitted"
    assert q["reason"] != "unclear"
    assert any(
        e.get("reason") == "output_not_submitted"
        for e in (q.get("excluded_claims") or [])
    )
    assert fin.payload.get("report_artifact_id") or fin.payload.get("minimal_report")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_e2e_agent_put_two_independent_mets_supported(
    tmp_path, clock, runtime_cls
):
    """Live supported: agent put + core SESOI eval, two independent mets."""
    from datetime import UTC, datetime

    from coscientist.core.claims.graph import GraphEdge, GraphNode
    from coscientist.core.claims.verdict import hypothesis_verdict
    from coscientist.core.events.ids import experiment_node_id, hypothesis_node_id
    from coscientist.core.execution.artifacts import make_tool_ref

    sid = "st_agent_sup"
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
        supported_needed=2,
        refutations_needed=1,
        refutation_priority=True,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="P1",
                title="raw1",
                goal="d1",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="artifact_present")],
                addresses=["Q1"],
                expected_outputs=[
                    OutputSpec(name="main", media_type="application/json", role="data")
                ],
            ),
            TaskSpec(
                task_key="P2",
                title="raw2",
                goal="d2",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="artifact_present")],
                addresses=["Q1"],
                expected_outputs=[
                    OutputSpec(name="main", media_type="application/json", role="data")
                ],
            ),
            TaskSpec(
                task_key="E1",
                title="exp1",
                goal="g1",
                route="experiment",
                depends_on=["P1"],
                inputs=[InputSpec(from_task="P1", output="main")],
                criteria=[RequirementRef(template_id="value_match_sesoi")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E2",
                title="exp2",
                goal="g2",
                route="experiment",
                depends_on=["P2"],
                inputs=[InputSpec(from_task="P2", output="main")],
                criteria=[RequirementRef(template_id="value_match_sesoi")],
                addresses=["Q1"],
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="why", final_type="verdict")],
    )
    now = datetime.now(UTC)
    hid = hypothesis_node_id(sid, "H1")
    rt.graph.upsert_node(
        GraphNode(
            node_id=hid,
            kind="hypothesis",
            study_id=sid,
            ref_id="H1",
            attrs={"text": "h"},
        )
    )
    for tk in ("E1", "E2"):
        eid = experiment_node_id(sid, tk)
        if rt.graph.get_node(eid) is None:
            rt.graph.upsert_node(
                GraphNode(
                    node_id=eid,
                    kind="experiment",
                    study_id=sid,
                    ref_id=tk,
                    attrs={"task_key": tk},
                )
            )
        rt.graph.add_edge(
            GraphEdge(
                edge_id=f"tests:{eid}:{hid}",
                kind="tests",
                study_id=sid,
                src_id=eid,
                dst_id=hid,
                attrs={"intent": "refute"},
                created_at=now,
            )
        )

    for tk, blob in (("P1", b'{"raw": 1}'), ("P2", b'{"raw": 2}')):
        aid = await rt.start_attempt(tk)
        art = arts.put(
            blob,
            make_tool_ref(f"{aid}:1"),
            name="main",
            media_type="application/json",
            role="data",
        )
        await rt.executor.record(
            aid,
            AgentTaskOutput(
                summary="raw",
                outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
                claims=[],
            ),
        )

    for tk, est in (("E1", 2.0), ("E2", 3.0)):
        aid = await rt.start_attempt(tk)
        payload = f'{{"effect": {est}}}'.encode()
        art = arts.put(
            payload,
            make_tool_ref(f"{aid}:1"),
            name="main",
            media_type="application/json",
            role="data",
        )
        assert art.producer == "agent"
        await rt.executor.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
                values={"effect": est},
                claims=[{
                    "id": f"c_{tk}",
                    "target": "H1",
                    "target_kind": "hypothesis",
                    "checked_against": "value_match_sesoi",
                }],
            ),
        )
    by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
    hyps = by["hypothesis:H1"]
    assert len(hyps) == 2
    assert all((c.effect or {}).get("trusted") is False for c in hyps)
    assert all((c.effect or {}).get("countable") is True for c in hyps)
    assert all(c.severe for c in hyps)
    v, _, _, rule = hypothesis_verdict(
        hyps, True, supported_needed=2, refutations_needed=1, refutation_priority=True
    )
    assert v == "supported" and rule == "support_min"

    await rt.start_attempt("REPORT")
    await rt.cancel_task("REPORT", reason="cancelled")
    await rt.tick()
    assert rt._finalized
    events = await rt.store.read_all()
    fin = next(e for e in events if e.type == "StudyFinalized")
    hyp_rows = fin.payload.get("hypothesis_verdicts") or {}
    assert hyp_rows.get("H1", {}).get("verdict") == "supported"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_e2e_sandbox_metric_trusted_tag_and_countable(
    tmp_path, clock, runtime_cls
):
    """Sandbox put stamps trusted=True; still countable for verdicts (PR17 path)."""
    from datetime import UTC, datetime

    from coscientist.core.claims.dispositions import dispositions
    from coscientist.core.claims.graph import GraphEdge, GraphNode
    from coscientist.core.claims.verdict import hypothesis_verdict
    from coscientist.core.events.ids import experiment_node_id, hypothesis_node_id
    from coscientist.core.execution.artifacts import make_tool_ref

    sid = "st_sandbox"
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
        supported_needed=2,
        refutations_needed=1,
        refutation_priority=True,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="P1",
                title="raw1",
                goal="d1",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="artifact_present")],
                addresses=["Q1"],
                expected_outputs=[
                    OutputSpec(name="main", media_type="application/json", role="data")
                ],
            ),
            TaskSpec(
                task_key="P2",
                title="raw2",
                goal="d2",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="artifact_present")],
                addresses=["Q1"],
                expected_outputs=[
                    OutputSpec(name="main", media_type="application/json", role="data")
                ],
            ),
            TaskSpec(
                task_key="E1",
                title="s1",
                goal="g1",
                route="experiment",
                depends_on=["P1"],
                inputs=[InputSpec(from_task="P1", output="main")],
                criteria=[RequirementRef(template_id="value_match_sesoi")],
                addresses=["Q1"],
                expected_outputs=[
                    OutputSpec(name="main", media_type="application/json", role="data")
                ],
            ),
            TaskSpec(
                task_key="E2",
                title="s2",
                goal="g2",
                route="experiment",
                depends_on=["P2"],
                inputs=[InputSpec(from_task="P2", output="main")],
                criteria=[RequirementRef(template_id="value_match_sesoi")],
                addresses=["Q1"],
                expected_outputs=[
                    OutputSpec(name="main", media_type="application/json", role="data")
                ],
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="why", final_type="verdict")],
    )
    # Hypothesis + tests.intent=refute so capable refute counts toward supported.
    now = datetime(2026, 1, 1, tzinfo=UTC)
    hid = hypothesis_node_id(sid, "H1")
    rt.graph.upsert_node(
        GraphNode(node_id=hid, kind="hypothesis", study_id=sid, ref_id="H1", attrs={})
    )
    for tk in ("E1", "E2"):
        eid = experiment_node_id(sid, tk)
        if rt.graph.get_node(eid) is None:
            rt.graph.upsert_node(
                GraphNode(
                    node_id=eid,
                    kind="experiment",
                    study_id=sid,
                    ref_id=tk,
                    attrs={"task_key": tk},
                )
            )
        rt.graph.add_edge(
            GraphEdge(
                edge_id=f"tests:{eid}:{hid}",
                kind="tests",
                study_id=sid,
                src_id=eid,
                dst_id=hid,
                attrs={"intent": "refute"},
                created_at=now,
            )
        )

    for tk, blob in (("P1", b'{"raw": 1}'), ("P2", b'{"raw": 2}')):
        aid = await rt.start_attempt(tk)
        art = arts.put_sandbox(
            blob,
            make_tool_ref(f"{aid}:1"),
            name="main",
            media_type="application/json",
            role="data",
            net_isolation="hard",
            fs_isolation="hard",
        )
        await rt.executor.record(
            aid,
            AgentTaskOutput(
                summary="raw",
                outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
                claims=[],
            ),
        )

    for tk, est in (("E1", 2.0), ("E2", 3.0)):
        aid = await rt.start_attempt(tk)
        payload = f'{{"effect": {est}}}'.encode()
        art = arts.put_sandbox(
            payload,
            make_tool_ref(f"{aid}:1"),
            name="main",
            media_type="application/json",
            role="data",
            net_isolation="hard",
            fs_isolation="hard",
        )
        assert art.producer == "sandbox"
        await rt.executor.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
                values={"effect": est},
                claims=[{
                    "id": f"c_{tk}",
                    "target": "H1",
                    "target_kind": "hypothesis",
                    "checked_against": "value_match_sesoi",
                }],
            ),
        )
    by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
    hyps = by["hypothesis:H1"]
    assert len(hyps) == 2
    assert all((c.effect or {}).get("trusted") is True for c in hyps)
    assert all((c.effect or {}).get("countable") is True for c in hyps)
    assert all(c.severe for c in hyps)
    v, _, _, rule = hypothesis_verdict(
        hyps, True, supported_needed=2, refutations_needed=1, refutation_priority=True
    )
    assert v == "supported" and rule == "support_min"

    # Separate study: sandbox failing metric → refuted + trusted tag.
    sid2 = "st_sandbox_ref"
    rt2 = await open_runtime(
        tmp_path / "ref", sid2, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt2.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid2,
        finalize_reserve_s=5,
    )
    _, arts2 = attach_executor(rt2, tmp_path / "ref")
    for key in ("SPEC", "HYP", "LIT"):
        await rt2.start_attempt(key)
        await rt2.cancel_task(key, reason="cancelled")
    await rt2.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="falsify",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match", required=True)],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why", final_type="verdict")],
    )
    aid_f = await rt2.start_attempt("E0", provenance="real")
    art_f = arts2.put_sandbox(
        b'{"count": 999}',
        make_tool_ref(f"{aid_f}:1"),
        name="main",
        media_type="application/json",
        net_isolation="hard",
        fs_isolation="hard",
        role="data",
    )
    await rt2.executor.record(
        aid_f,
        AgentTaskOutput(
            summary="falsified",
            outputs={"main": ArtifactHandle(artifact_id=art_f.artifact_id, name="main")},
            values={"count": 999},
            claims=[{"id": "ref", "target": "Q1", "target_kind": "question", "checked_against": "value_match"}],
        ),
    )
    by2 = filtered_claims_by_target(rt2.graph, study_id=sid2, results=rt2.results)
    c_fail = by2["question:Q1"][0]
    assert (c_fail.effect or {}).get("trusted") is True
    assert (c_fail.effect or {}).get("countable") is True
    assert c_fail.claim_outcome == "inconsistent"
    d2 = dispositions(
        tasks=rt2.tasks,
        plans=rt2.plans,
        graph=rt2.graph,
        study=rt2.study,
        results=rt2.results,
    )
    assert d2["Q1"]["verdict"] == "refuted"
    await close_runtime(rt)
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_verdict_replay_equals_finalized_snapshots(tmp_path, clock, runtime_cls):
    """V1: journaled VerdictSnapshotsRecorded wins on reopen (not recomputed softer)."""
    from coscientist.core.execution.artifacts import make_tool_ref

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
        questions=[QuestionSpec(id="Q1", text="why", final_type="verdict")],
    )
    aid0 = await rt.start_attempt("E0")
    art = arts.put(
        b'{"effect": 2.0}',
        make_tool_ref(f"{aid0}:1"),
        name="main",
        media_type="application/json",
        role="data",
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"effect": 2.0},
            claims=[{"id": "c1", "target": "Q1", "target_kind": "question", "checked_against": "value_match_sesoi"}],
        ),
    )
    # Bind E1 → records VerdictSnapshotsRecorded for Q1.
    await rt.start_attempt("E1")
    snap1 = dict(rt.verdict_snapshots.get("Q1") or {})
    # Agent put + core SESOI eval → countable severe → survived (k_sup=2).
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


@pytest.mark.asyncio
async def test_forged_severe_refute_capable_recomputed_on_replay(
    tmp_path, clock, runtime_cls
):
    """Legacy/forged severe=True / refute_capable=True must not survive read/replay."""
    from coscientist.core.claims.graph import GraphEdge, GraphNode
    from coscientist.core.events.events import EventEnvelope, make_idem
    from coscientist.core.events.ids import claim_node_id, question_node_id

    sid = "st_forge"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="artifact_present")],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why", final_type="verdict")],
    )
    cid = claim_node_id(sid, "forged")
    qid = question_node_id(sid, "Q1")
    aid = f"{sid}/E0#1"
    await rt._append(
        [
            EventEnvelope(
                study_id=sid,
                type="GraphNodeUpserted",
                entity_key=cid,
                idem_key=make_idem("GraphNodeUpserted", cid, "forge"),
                payload=GraphNode(
                    node_id=cid,
                    kind="claim",
                    study_id=sid,
                    ref_id="forged",
                    attrs={
                        "provenance": "real",
                        "attempt_id": aid,
                        "attempt_outcome": "SUCCESS",
                        "target": "Q1",
                        "accepted": True,
                        "criterion_status": "met",
                        "claim_outcome": "consistent",
                        "frozen": True,
                        "evidence_kind": "computational",
                        "validity_ok": True,
                        "check_role": "prediction",
                        # Forged legacy fields — must be ignored / recomputed False.
                        "severe": True,
                        "refute_capable": True,
                        "independence_key": "forged-key",
                        "experiment_id": f"{sid}/E0",
                        # Forged trusted without core producer → recomputed False.
                        "effect": {
                            "est": 9.0,
                            "kind": "point",
                            "trusted": True,
                            "producer": "agent",
                        },
                        "uses_sesoi": True,
                        "used_sesoi": True,
                        "sesoi": 0.5,
                    },
                ).model_dump(mode="json"),
                ts=clock.now(),
            ),
            EventEnvelope(
                study_id=sid,
                type="GraphEdgeAdded",
                entity_key=f"{cid}->about->{qid}",
                idem_key=make_idem("GraphEdgeAdded", cid, "about"),
                payload=GraphEdge(
                    edge_id=f"{cid}:about:{qid}",
                    kind="about",
                    src_id=cid,
                    dst_id=qid,
                    study_id=sid,
                ).model_dump(mode="json"),
                ts=clock.now(),
            ),
        ]
    )
    by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
    claim = by["question:Q1"][0]
    assert claim.severe is False
    assert claim.refute_capable is False
    assert (claim.effect or {}).get("trusted") is False
    assert (claim.effect or {}).get("producer") == "agent"
    assert claim_is_severe(claim) is False
    assert refute_intent_counts(claim) is False

    await close_runtime(rt)
    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    by2 = filtered_claims_by_target(rt2.graph, study_id=sid, results=rt2.results)
    claim2 = by2["question:Q1"][0]
    assert claim2.severe is False
    assert claim2.refute_capable is False
    assert (claim2.effect or {}).get("trusted") is False
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_e2e_tost_agent_metric_countable_severe(tmp_path, clock, runtime_cls):
    """Agent put + core TOST eval → countable severe; trusted stays false."""
    from coscientist.core.execution.artifacts import make_tool_ref

    sid = "st_tost"
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
                title="tost",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="tost_equivalence")],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why", final_type="verdict")],
    )
    aid0 = await rt.start_attempt("E0")
    art = arts.put(
        b'{"effect": {"est": 0.0, "ci_low": -0.1, "ci_high": 0.1}}',
        make_tool_ref(f"{aid0}:1"),
        name="main",
        media_type="application/json",
        role="data",
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={},
            claims=[{"id": "c1", "target": "Q1", "target_kind": "question", "checked_against": "tost_equivalence"}],
        ),
    )
    by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
    c = by["question:Q1"][0]
    assert c.claim_outcome == "consistent"
    assert c.used_sesoi is True
    assert (c.effect or {}).get("trusted") is False
    assert (c.effect or {}).get("countable") is True
    assert c.severe is True
    # refute_capable = SESOI setup could fail; intent is separate (default confirm).
    assert c.refute_capable is True
    assert c.intent == "confirm"
    await close_runtime(rt)
