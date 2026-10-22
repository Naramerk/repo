import pytest
from refmodel import Claim, Graph, PlanInvalid, costly_gate, edge_allowed, study_fulfilled, validate_plan, verdict


def test_edge_pairs_enforced():
    assert edge_allowed("derived_from", "evidence", "artifact")
    assert not edge_allowed("derived_from", "evidence", "plan")
    assert not edge_allowed("derived_from", "plan", "plan")       # plan revisions use supersedes
    assert edge_allowed("supersedes", "plan", "plan")
    assert not edge_allowed("produced", "plan", "artifact")


def _plan(questions=("Q1",), cover=("Q1",)):
    gr = Graph()
    gr.add("ST", "study")
    for q in questions:
        gr.add(q, "question")
    gr.add("PL", "plan")
    for i, q in enumerate(cover):
        x = gr.add(f"X{i}", "experiment", task_key=f"EXP-{i}")
        gr.link("plans", "PL", x)
        gr.link("addresses", x, q)
    return gr


def test_rt1_task_linked_to_plan():
    """RT1 (old completion.py:165): a task with no plan linkage must be rejected."""
    gr = _plan()
    validate_plan(gr, "PL")
    gr.add("X9", "experiment", task_key="EXP-9")                  # orphan task
    with pytest.raises(PlanInvalid, match="RT1"):
        validate_plan(gr, "PL")


def test_plan_rejected_when_question_uncovered():
    with pytest.raises(PlanInvalid, match="Q2"):
        validate_plan(_plan(questions=("Q1", "Q2"), cover=("Q1",)), "PL")


def test_h1_hypothesis_gets_verdict():
    """H1 (old completion.py:95): study closed with untested hypothesis. Done experiments ⇒ verdict ≠ untested."""
    assert verdict([], any_experiment_done=True)[0] == "inconclusive"
    assert verdict([Claim("met")], any_experiment_done=True)[0] == "supported"
    assert not study_fulfilled({"Q1": "untested"}, all_required_success=True)
    assert study_fulfilled({"Q1": "refuted"}, all_required_success=True)


def test_stance_computed_from_criterion():
    assert [Claim(s).stance for s in ("met", "not_met", "unassessed")] == ["supports", "refutes", "inconclusive"]


def test_k_confirmations_need_distinct_attempts_and_artifacts():
    same = [Claim("met"), Claim("met")]
    assert verdict(same, True, k=2)[0] == "inconclusive"
    two = [Claim("met"), Claim("met", attempt_id="X2#1", artifact_id="A2")]
    assert verdict(two, True, k=2)[0] == "supported"


def test_conflict_and_caps():
    assert verdict([Claim("met"), Claim("not_met", attempt_id="X2#1", artifact_id="A2")], True)[0] == "inconclusive"
    assert verdict([Claim("met", provenance="simulated")], True)[0] == "inconclusive"
    # cap is over counted claims: an uncounted non-real claim does not cap
    extra = Claim("met", provenance="simulated", attempt_outcome="PARTIAL", attempt_id="X3#1", artifact_id="A3")
    assert verdict([Claim("met"), extra], True)[0] == "supported"


def test_literature_verdict_flag():
    lit = Claim("met", evidence_kind="literature", lit_own_criterion=True)
    assert verdict([lit], True) == ("supported", True)
    assert verdict([Claim("met", evidence_kind="literature")], True) == ("inconclusive", False)
    assert verdict([], True) == ("inconclusive", False)              # all() on empty is not "по литературе"


def test_paper_dedupe_two_lits_same_doi():
    gr = Graph()
    gr.add("LIT0", "experiment", task_key="LIT-0")
    gr.add("LIT1", "experiment", task_key="LIT-1")
    a = gr.add_paper("LIT0", "10.1000/XYZ")
    b = gr.add_paper("LIT1", "https://doi.org/10.1000/xyz")
    assert a == b and len(gr.of("artifact")) == 1 and gr.inc("produced", a) == ["LIT0"]


@pytest.mark.parametrize("mode,answer,cost,left,effect,by", [
    ("fail", None, 0, 0, "FAILED(human_timeout)", None),
    ("fail", "delegate", 0, 0, "WAIT", None),
    ("delegate_capped", None, 500, 1000, "RUN", "critic"),
    ("delegate_capped", "delegate", 1500, 1000, "SKIPPED(budget)", "critic"),
    ("delegate", None, 10**6, 0, "RUN", "critic"),
    ("delegate_capped", "approve", 10**6, 0, "RUN", "human"),
])
def test_costly_gate_modes(mode, answer, cost, left, effect, by):
    eff, ev = costly_gate(mode, answer, cost, left)
    assert eff == effect and ev["decided_by"] == by and ev["reason"]
    assert "spent_rub" in ev
    if eff == "RUN" and by == "critic":
        assert ev["spent_rub"] == cost


@pytest.mark.xfail(reason="runtime-only: needs event log + recover(); covered by M0 PR #1 (cursor/m0-state-core-38bb)",
                   strict=False)
def test_recovery_stage_task_not_rerun():
    from coscientist_next.core import recover  # noqa: F401  (M0)
    raise NotImplementedError


@pytest.mark.xfail(reason="runtime-only: Budget(delegated_costly) accounting across attempts; M0 PR #1", strict=False)
def test_costly_gate_budget_spent_shown_in_report():
    from coscientist_next.report import render  # noqa: F401  (M5)
    raise NotImplementedError
