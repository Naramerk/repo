import gen_model_tables as g
import pydantic
import pytest


def test_edge_pairs_enforced(impl):
    assert impl.edge_allowed("derived_from", "evidence", "artifact")
    assert not impl.edge_allowed("derived_from", "evidence", "plan")
    assert not impl.edge_allowed("derived_from", "plan", "plan")  # plan revisions use supersedes
    assert impl.edge_allowed("supersedes", "plan", "plan")
    assert not impl.edge_allowed("produced", "plan", "artifact")


def _plan(impl, questions=("Q1",), cover=("Q1",)):
    gr = impl.Graph()
    gr.add("ST", "study")
    for q in questions:
        gr.add(q, "question")
    gr.add("PL", "plan")
    for i, q in enumerate(cover):
        x = gr.add(f"X{i}", "experiment", task_key=f"EXP-{i}")
        gr.link("plans", "PL", x)
        gr.link("addresses", x, q)
    return gr


def test_rt1_task_linked_to_plan(impl):
    """RT1 (old completion.py:165): a task with no plan linkage must be rejected."""
    gr = _plan(impl)
    impl.validate_plan(gr, "PL")
    gr.add("X9", "experiment", task_key="EXP-9")  # orphan task
    with pytest.raises(impl.PlanInvalid, match="RT1"):
        impl.validate_plan(gr, "PL")


def test_plan_rejected_when_question_uncovered(impl):
    with pytest.raises(impl.PlanInvalid, match="Q2"):
        impl.validate_plan(_plan(impl, questions=("Q1", "Q2"), cover=("Q1",)), "PL")


def test_h1_hypothesis_gets_verdict(impl):
    """H1: done experiments ⇒ verdict ≠ untested. PR8b: single severe confirm → survived."""
    # PR8b: no claims about target → untested (even if experiments ran).
    assert (
        impl.verdict(
            [],
            any_experiment_done=True,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            confirmations_needed=1,
        )[0]
        == "untested"
    )
    assert (
        impl.verdict(
            [impl.Claim("met")],
            any_experiment_done=True,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            confirmations_needed=1,
        )[0]
        == "survived"
    )
    assert not impl.study_fulfilled({"Q1": "untested"}, all_required_success=True)
    assert impl.study_fulfilled({"Q1": "refuted"}, all_required_success=True)


def test_stance_computed_from_criterion(impl):
    claim_in = g.models()["claim"]
    with pytest.raises(pydantic.ValidationError, match="stance"):
        claim_in(target="H1", checked_against="CR1", stance="supports")  # role may not set stance
    c = claim_in(target="H1", checked_against="CR1")
    assert not hasattr(c, "stance")
    assert [impl.claim_stance(s) for s in ("met", "not_met", "unassessed")] == [
        "supports",
        "refutes",
        "inconclusive",
    ]
    assert [impl.Claim(s).stance for s in ("met", "not_met", "unassessed")] == [
        "supports",
        "refutes",
        "inconclusive",
    ]
    # Role criterion model mirrors KindSpec: code without metric/op/target refuses.
    criterion_in = g.models()["criterion"]
    with pytest.raises(pydantic.ValidationError, match="metric"):
        criterion_in(check="code")
    ok = criterion_in(check="code", metric="n", op=">=", target="1")
    assert ok.root.metric == "n"


def test_supported_needs_distinct_independence_keys(impl):
    """PR8b: supported needs supported_needed severe + ≥1 capable refute intent."""
    same = [impl.Claim("met", intent="refute"), impl.Claim("met", intent="refute")]
    # Same independence_key → one key → survived.
    assert (
        impl.verdict(
            same,
            True,
            supported_needed=2,
            refutations_needed=1,
            refutation_priority=False,
            confirmations_needed=1,
        )[0]
        == "survived"
    )
    two = [
        impl.Claim("met", intent="refute", attempt_id="X1#1", artifact_id="A1"),
        impl.Claim("met", intent="confirm", attempt_id="X2#1", artifact_id="A2"),
    ]
    assert (
        impl.verdict(
            two,
            True,
            supported_needed=2,
            refutations_needed=1,
            refutation_priority=False,
            confirmations_needed=1,
        )[0]
        == "supported"
    )


def test_conflict_and_caps(impl):
    conflict = [impl.Claim("met"), impl.Claim("not_met", attempt_id="X2#1", artifact_id="A2")]
    # Conflict row requires ¬refutation_priority (hypothesis_verdict default is True).
    assert (
        impl.verdict(
            conflict,
            True,
            refutation_priority=False,
            refutations_needed=1,
            supported_needed=2,
            confirmations_needed=1,
        )[0]
        == "inconclusive"
    )
    # PR8b: stub excluded before counting → no evidence → inconclusive.
    assert (
        impl.verdict(
            [impl.Claim("met", provenance="stub")],
            True,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            confirmations_needed=1,
        )[0]
        == "inconclusive"
    )
    # Stub does not affect a real severe confirm (survived under PR8b).
    extra = impl.Claim("met", provenance="stub", attempt_outcome="PARTIAL", attempt_id="X3#1", artifact_id="A3")
    assert (
        impl.verdict(
            [impl.Claim("met"), extra],
            True,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            confirmations_needed=1,
        )[0]
        == "survived"
    )


@pytest.mark.parametrize(
    "claims_factory, expect",
    [
        (
            lambda C: [C("met", evidence_kind="literature", lit_own_criterion=True)],
            ("inconclusive", False),
        ),
        (
            lambda C: [C("met", evidence_kind="literature")],
            ("inconclusive", False),
        ),
        (lambda C: [], ("untested", False)),
    ],
    ids=["lit_own_criterion", "plain_lit", "empty"],
)
def test_literature_verdict_flag(impl, claims_factory, expect):
    """PR8b: literature never counts (thresholds/priors only)."""
    # 4-tuple from hypothesis_verdict: (verdict, by_lit, by_theory, rule)
    v = impl.verdict(
        claims_factory(impl.Claim),
        True,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        confirmations_needed=1,
    )
    assert v[:2] == expect


def test_paper_dedupe_two_lits_same_doi():
    """Refmodel-only: exercises ``Graph.add_paper`` (oracle helper).

    Not routed through the ``impl`` fixture (RC3t dropped the dead refmodel branch).
    """
    import refmodel

    gr = refmodel.Graph()
    gr.add("LIT0", "experiment", task_key="LIT-0")
    gr.add("LIT1", "experiment", task_key="LIT-1")
    a = gr.add_paper("LIT0", "10.1000/XYZ")
    b = gr.add_paper("LIT1", "https://doi.org/10.1000/xyz")
    assert a == b and len(gr.of("artifact")) == 1 and gr.inc("produced", a) == ["LIT0"]


def _full_graph(impl):
    gr = impl.Graph()
    for nid, kind in [
        ("ST", "study"),
        ("Q1", "question"),
        ("CR0", "criterion"),
        ("PL", "plan"),
        ("X0", "experiment"),
        ("A1", "artifact"),
        ("E1", "evidence"),
        ("CL1", "claim"),
        ("CN1", "conclusion"),
        ("RP", "report"),
    ]:
        gr.add(nid, kind)
    for e in [
        ("asks", "ST", "Q1"),
        ("measured_by", "ST", "CR0"),
        ("plans", "PL", "X0"),
        ("addresses", "X0", "Q1"),
        ("produced", "X0", "A1"),
        ("derived_from", "E1", "A1"),
        ("cites", "CL1", "E1"),
        ("about", "CL1", "Q1"),
        ("answers", "CN1", "Q1"),
        ("based_on", "CN1", "CL1"),
        ("renders", "RP", "CN1"),
    ]:
        gr.link(*e)
    return gr


def test_check_cardinality(impl):
    gr = _full_graph(impl)
    assert impl.check_cardinality(gr) == []
    gr.add("E2", "evidence")  # evidence without derived_from
    gr.link("measured_by", "X0", "CR0")  # criterion measured twice
    bad = impl.check_cardinality(gr)
    assert "derived_from out E2=0" in bad and "measured_by in CR0=2" in bad


@pytest.mark.asyncio
async def test_recovery_stage_task_not_rerun(tmp_path, clock, runtime_cls):
    """Task with ResultRecorded inside a stage: recover adds 0 attempts and 0 nodes."""
    from datetime import timedelta

    from coscientist.core.events.ids import result_id
    from coscientist.core.execution.domain import TaskState
    from coscientist.core.execution.recover import recover
    from tests.helpers import attach_executor, close_runtime, open_runtime, record_success

    rt = await open_runtime(tmp_path, "st_rec_mr", clock=clock, runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_rec_mr",
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    nodes_before = len(rt.graph.all_nodes())
    _, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await record_success(rt, aid, arts)
    assert result_id(aid) in rt.results or aid in rt.results
    n_attempts_before = len(rt.tasks["st_rec_mr/SPEC"].attempts)
    await recover(rt)
    assert len(rt.tasks["st_rec_mr/SPEC"].attempts) == n_attempts_before
    assert rt.tasks["st_rec_mr/SPEC"].state == TaskState.DONE
    assert len(rt.graph.all_nodes()) == nodes_before
    starts = [e for e in await rt.store.read_all() if e.type == "AttemptStarted"]
    assert len(starts) == 1
    await close_runtime(rt)


@pytest.mark.parametrize(
    "required_ok,verdicts",
    [(True, {"Q1": "refuted"}), (True, {"Q1": "untested"}), (False, {"Q1": "supported"})],
)
def test_core_study_view_matches_refmodel(required_ok, verdicts):
    import refmodel

    core = pytest.importorskip(
        "coscientist.core.claims.rules", reason="coscientist.core.claims.rules not present yet (M0)"
    )
    got = core.study_view_verdict(required_ok, verdicts) == "fulfilled"
    assert got == refmodel.study_fulfilled(verdicts, required_ok)
