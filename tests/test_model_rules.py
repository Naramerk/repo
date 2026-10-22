import gen_model_tables as g
import pydantic
import pytest


def test_edge_pairs_enforced(impl):
    assert impl.edge_allowed("derived_from", "evidence", "artifact")
    assert not impl.edge_allowed("derived_from", "evidence", "plan")
    assert not impl.edge_allowed("derived_from", "plan", "plan")       # plan revisions use supersedes
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
    gr.add("X9", "experiment", task_key="EXP-9")                  # orphan task
    with pytest.raises(impl.PlanInvalid, match="RT1"):
        impl.validate_plan(gr, "PL")


def test_plan_rejected_when_question_uncovered(impl):
    with pytest.raises(impl.PlanInvalid, match="Q2"):
        impl.validate_plan(_plan(impl, questions=("Q1", "Q2"), cover=("Q1",)), "PL")


def test_h1_hypothesis_gets_verdict(impl):
    """H1 (old completion.py:95): study closed with untested hypothesis. Done experiments ⇒ verdict ≠ untested."""
    assert impl.verdict([], any_experiment_done=True)[0] == "inconclusive"
    assert impl.verdict([impl.Claim("met")], any_experiment_done=True)[0] == "supported"
    assert not impl.study_fulfilled({"Q1": "untested"}, all_required_success=True)
    assert impl.study_fulfilled({"Q1": "refuted"}, all_required_success=True)


def test_stance_computed_from_criterion(impl):
    claim_in = g.models()["claim"]
    with pytest.raises(pydantic.ValidationError, match="stance"):
        claim_in(target="H1", checked_against="CR1", stance="supports")        # role may not set stance
    c = claim_in(target="H1", checked_against="CR1")
    assert not hasattr(c, "stance")
    assert [impl.Claim(s).stance for s in ("met", "not_met", "unassessed")] == ["supports", "refutes", "inconclusive"]


def test_k_confirmations_need_distinct_attempts_and_artifacts(impl):
    same = [impl.Claim("met"), impl.Claim("met")]
    assert impl.verdict(same, True, k=2)[0] == "inconclusive"
    two = [impl.Claim("met"), impl.Claim("met", attempt_id="X2#1", artifact_id="A2")]
    assert impl.verdict(two, True, k=2)[0] == "supported"


def test_conflict_and_caps(impl):
    conflict = [impl.Claim("met"), impl.Claim("not_met", attempt_id="X2#1", artifact_id="A2")]
    assert impl.verdict(conflict, True)[0] == "inconclusive"
    assert impl.verdict([impl.Claim("met", provenance="simulated")], True)[0] == "inconclusive"
    # cap is over counted claims: an uncounted non-real claim does not cap
    extra = impl.Claim("met", provenance="simulated", attempt_outcome="PARTIAL", attempt_id="X3#1", artifact_id="A3")
    assert impl.verdict([impl.Claim("met"), extra], True)[0] == "supported"


def test_literature_verdict_flag(impl):
    lit = impl.Claim("met", evidence_kind="literature", lit_own_criterion=True)
    assert impl.verdict([lit], True) == ("supported", True)
    assert impl.verdict([impl.Claim("met", evidence_kind="literature")], True) == ("inconclusive", False)
    assert impl.verdict([], True) == ("inconclusive", False)              # all() on empty is not "по литературе"


def test_paper_dedupe_two_lits_same_doi(impl):
    gr = impl.Graph()
    gr.add("LIT0", "experiment", task_key="LIT-0")
    gr.add("LIT1", "experiment", task_key="LIT-1")
    a = gr.add_paper("LIT0", "10.1000/XYZ")
    b = gr.add_paper("LIT1", "https://doi.org/10.1000/xyz")
    assert a == b and len(gr.of("artifact")) == 1 and gr.inc("produced", a) == ["LIT0"]


@pytest.mark.parametrize("mode,answer,cost,limit,effect,by,msg", [
    ("fail", None, 0, 1000, "FAILED(human_timeout)", None, None),
    ("fail", "delegate", 100, None, "WAIT", None, "нет бюджета"),
    ("fail", "delegate", 1500, 1000, "WAIT", None, "превышает остаток"),
    ("fail", "delegate", 500, 1000, "RUN", "critic", None),
    ("delegate_capped", None, 500, 1000, "RUN", "critic", None),
    ("delegate_capped", "delegate", 1500, 1000, "SKIPPED(budget)", "critic", "превышает остаток"),
    ("delegate", None, 10**6, None, "RUN", "critic", None),
    ("delegate_capped", "approve", 10**6, None, "RUN", "human", None),
])
def test_costly_gate_modes(impl, mode, answer, cost, limit, effect, by, msg):
    budget = None if limit is None else impl.DelegatedBudget(limit)
    eff, ev = impl.costly_gate(mode, answer, cost, budget)
    assert eff == effect and ev["decided_by"] == by and ev["reason"]
    assert ev.get("human_message") == msg
    if eff == "RUN" and by == "critic":
        assert ev["delegated_by"] == ("human" if answer == "delegate" else "timeout")
        impl.settle(ev, budget, cost)
        assert ev["spent_rub"] == cost


def test_delegated_budget_counter(impl):
    b = impl.DelegatedBudget(1000)
    eff, ev = impl.costly_gate("fail", "delegate", 400, b)
    assert eff == "RUN" and ev["delegated_by"] == "human"
    impl.settle(ev, b, 300)                                          # actual debited, not estimate
    assert (b.spent_rub, b.remaining_rub, ev["spent_rub"]) == (300, 700, 300)
    eff, ev = impl.costly_gate("fail", "delegate", 800, b)
    assert eff == "WAIT" and ev["human_message"] == "превышает остаток"
    eff, ev = impl.costly_gate("fail", "delegate", 200, b)
    impl.settle(ev, b, 350)                                          # overspend: actual > estimate
    assert b.blocked and b.remaining_rub == 350
    eff, ev = impl.costly_gate("fail", "delegate", 10, b)
    assert eff == "WAIT" and ev["reason"] == "delegation_disabled" and "перерасход" in ev["human_message"]
    assert impl.costly_gate("fail", None, 10, b)[0] == "FAILED(human_timeout)"


def _full_graph(impl):
    gr = impl.Graph()
    for nid, kind in [("ST", "study"), ("Q1", "question"), ("CR0", "criterion"), ("PL", "plan"),
                      ("X0", "experiment"), ("A1", "artifact"), ("E1", "evidence"),
                      ("CL1", "claim"), ("CN1", "conclusion"), ("RP", "report")]:
        gr.add(nid, kind)
    for e in [("asks", "ST", "Q1"), ("measured_by", "ST", "CR0"), ("plans", "PL", "X0"), ("addresses", "X0", "Q1"),
              ("produced", "X0", "A1"), ("derived_from", "E1", "A1"), ("cites", "CL1", "E1"), ("about", "CL1", "Q1"),
              ("answers", "CN1", "Q1"), ("based_on", "CN1", "CL1"), ("renders", "RP", "CN1")]:
        gr.link(*e)
    return gr


def test_check_cardinality(impl):
    gr = _full_graph(impl)
    assert impl.check_cardinality(gr) == []
    gr.add("E2", "evidence")                                         # evidence without derived_from
    gr.link("measured_by", "X0", "CR0")                              # criterion measured twice
    bad = impl.check_cardinality(gr)
    assert "derived_from out E2=0" in bad and "measured_by in CR0=2" in bad


@pytest.mark.asyncio
async def test_recovery_stage_task_not_rerun(tmp_path, clock):
    """Task with ResultRecorded inside a stage: recover adds 0 attempts and 0 nodes."""
    from datetime import timedelta

    from coscientist.core.domain import TaskState
    from coscientist.core.ids import result_id
    from coscientist.core.recover import recover
    from coscientist.core.runtime_factory import close_runtime, open_runtime
    from tests.helpers import attach_executor, record_success

    rt = await open_runtime(tmp_path, "st_rec_mr", clock=clock)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_rec_mr",
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
    starts = [e for e in rt.store.read_all() if e.type == "AttemptStarted"]
    assert len(starts) == 1
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_costly_gate_budget_spent_shown_in_report(tmp_path, clock):
    """fail-mode «реши сам», DelegatedBudget debit via events, report shows spent_rub."""
    from datetime import timedelta
    from decimal import Decimal

    from coscientist.core.domain import Budget, GateConfig, HitlPolicy, TaskOutcome
    from coscientist.core.report import render
    from coscientist.core.runtime_factory import close_runtime, open_runtime
    from tests.helpers import attach_executor, record_success

    policy = HitlPolicy(
        mode="interactive",
        gates={
            "costly_or_nonreal": GateConfig(
                enabled=True, timeout_s=30, on_timeout="fail"
            )
        },
    )
    budget = Budget(
        delegated_costly=Decimal("1000"), deadline=clock.now() + timedelta(hours=1)
    )

    # No budget configured → WAIT + human_message, gate stays open
    rt0 = await open_runtime(tmp_path / "nb", "st_nb", clock=clock)
    await rt0.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_nb",
        hitl_policy=policy,
    )
    _, arts0 = attach_executor(rt0, tmp_path / "nb")
    await record_success(rt0, await rt0.start_attempt("SPEC"), arts0)
    rid0 = await rt0.open_hitl("PLAN", kind="costly_or_nonreal", cost_rub=100)
    await rt0.answer_hitl(rid0, "delegate")
    assert rt0.hitl[rid0].open is True
    note0 = [e for e in rt0.store.read_all() if e.payload.get("request_id") == rid0]
    assert any(e.payload.get("reason") == "delegation_disabled" for e in note0)
    assert any(e.payload.get("human_message") == "нет бюджета" for e in note0)
    await close_runtime(rt0)

    rt = await open_runtime(tmp_path, "st_cost", clock=clock)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_cost",
        hitl_policy=policy,
        budget=budget,
    )
    _, arts = attach_executor(rt, tmp_path)
    await record_success(rt, await rt.start_attempt("SPEC"), arts)

    # «реши сам» with budget → critic, delegated_by=human; settle debits actual
    rid = await rt.open_hitl("PLAN", kind="costly_or_nonreal", cost_rub=400)
    await rt.answer_hitl(rid, "delegate")
    assert rt.hitl[rid].open is False
    decided = [
        e
        for e in rt.store.read_all()
        if e.type == "HitlDecided" and e.payload.get("request_id") == rid
    ]
    assert decided and decided[-1].payload.get("delegated_by") == "human"
    settled = await rt.settle_delegated_spend(rid, actual_rub=350)
    assert settled["spent_rub"] == 350
    assert rt.delegated_budget is not None and rt.delegated_budget.spent_rub == 350
    report = render(rt.store.read_all())
    assert report["hitl_spend"]["total_spent_rub"] == 350.0
    assert any(d["spent_rub"] == 350.0 for d in report["hitl_spend"]["decisions"])
    assert "350" in report["summary"]

    # actual > estimate → blocked; further «реши сам» → WAIT
    rid2 = await rt.open_hitl("PLAN", kind="costly_or_nonreal", cost_rub=200)
    await rt.answer_hitl(rid2, "delegate")
    await rt.settle_delegated_spend(rid2, actual_rub=400)
    assert rt.delegated_budget.blocked
    rid3 = await rt.open_hitl("HYP", kind="costly_or_nonreal", cost_rub=10)
    await rt.answer_hitl(rid3, "delegate")
    assert rt.hitl[rid3].open is True
    notice = [e for e in rt.store.read_all() if e.payload.get("request_id") == rid3]
    assert any(e.payload.get("reason") == "delegation_disabled" for e in notice)
    assert any("перерасход" in (e.payload.get("human_message") or "") for e in notice)

    # timeout in fail mode → FAILED(human_timeout)
    rid4 = await rt.open_hitl("LIT", kind="costly_or_nonreal", timeout_s=1, cost_rub=10)
    clock.advance(timedelta(seconds=2))
    await rt.tick()
    assert rt.hitl[rid4].open is False
    assert rt.tasks["st_cost/LIT"].outcome == TaskOutcome.FAILED
    assert rt.tasks["st_cost/LIT"].reason == "human_timeout"
    await close_runtime(rt)


@pytest.mark.parametrize(
    "required_ok,verdicts",
    [(True, {"Q1": "refuted"}), (True, {"Q1": "untested"}), (False, {"Q1": "supported"})],
)
def test_core_study_view_matches_refmodel(required_ok, verdicts):
    import refmodel

    core = pytest.importorskip(
        "coscientist.core.rules", reason="coscientist.core.rules not present yet (M0)"
    )
    got = core.study_view_verdict(required_ok, verdicts) == "fulfilled"
    assert got == refmodel.study_fulfilled(verdicts, required_ok)
