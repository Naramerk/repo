"""HYP v2 registration + durable verdict fold (docs/plan/hypothesis-v2.md §3.2–§3.4).

Chain: record_hypotheses registers schema-validated hypothesis graph nodes →
accept_plan materializes question_ref as ``addresses`` edges → the finalize fold
stamps real claim-engine verdicts onto node attrs (graph digest reads them).
No new verdict vocabulary: insufficient evidence stays ``untested``/`inconclusive`
(the honest "unverifiable" of the claims engine).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coscientist.agents.schemas_hyp_lit import HypothesisDraft, HypothesisOut
from coscientist.cli.journal import record_hypotheses
from coscientist.core.claims.graph import GraphStore, default_execution_registry
from coscientist.core.execution.domain import ForEachSpec, Need, QuestionSpec, TaskSpec
from coscientist.core.execution.local_project import LocalProjectMixin
from tests.helpers import NEED_OFF, attach_executor, close_runtime, open_runtime

_NEED_ON = Need(kind="need", needed=True, reason="hypotheses required")


def _hyp_out(*, question_ref: str = "q1") -> HypothesisOut:
    return HypothesisOut(
        hypotheses=(
            HypothesisDraft(
                id="H0", statement="s0", rationale="r0",
                prediction="AD median >= 0.6", rank=0,
                question_ref=question_ref, selected=True,
            ),
            HypothesisDraft(
                id="H1", statement="s1", rationale="r1",
                prediction="AD median < 0.6 with structural gap >= 0.2", rank=1,
                question_ref=question_ref, selected=False,
            ),
            HypothesisDraft(
                id="H2", statement="s2", rationale="r2",
                prediction="AD median <= 0.5 across clusters", rank=2,
                question_ref=question_ref, selected=False,
            ),
        )
    )


async def _runtime_with_hyp_recorded(tmp_path, sid: str):
    rt = await open_runtime(tmp_path, sid)
    await rt.create_study(
        profile="t",
        request="q",
        catalog_version=4,
        deadline=rt.clock.now() + timedelta(seconds=400),
        require_hypotheses=True,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    attach_executor(rt, tmp_path)
    await rt.start_attempt("SPEC")
    await rt.cancel_task("SPEC", reason="cancelled")
    await rt.start_attempt("HYP")
    return rt


async def _aid_for(rt, task_key: str) -> str:
    for t in rt.tasks.values():
        if t.spec.task_key == task_key and t.attempts:
            return str(t.attempts[-1])
    raise AssertionError(f"no attempt on {task_key}")


@pytest.mark.asyncio
async def test_record_hypotheses_registers_v2_nodes(tmp_path) -> None:
    sid = "st_hypv2_nodes"
    rt = await _runtime_with_hyp_recorded(tmp_path, sid)
    try:
        aid = await _aid_for(rt, "HYP")
        await record_hypotheses(rt, aid, _hyp_out())
        nodes = rt.graph.nodes_by_kind("hypothesis")
        assert {n.ref_id for n in nodes} == {"H0", "H1", "H2"}
        by_id = {str(n.ref_id): n for n in nodes}
        h0 = by_id["H0"]
        assert h0.attrs["statement"] == "s0"
        assert h0.attrs["prediction"] == "AD median >= 0.6"
        assert h0.attrs["selected"] is True
        assert h0.attrs["rank"] == 0
        assert h0.attrs["question_ref"] == "q1"
        assert h0.attrs["source_task"] == "HYP"
        assert {n.attrs["source_task"] for n in nodes} == {"HYP"}
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_record_hypotheses_fails_closed_without_study_id(tmp_path) -> None:
    """No attempt study_id → typed refuse; no half registration."""
    sid = "st_hypv2_nostudy"
    rt = await _runtime_with_hyp_recorded(tmp_path, sid)
    try:
        attempts = dict(rt.attempts)
        aid = await _aid_for(rt, "HYP")
        broken = dict(attempts[aid])
        broken.pop("study_id")
        attempts[aid] = broken
        rt.attempts.clear()
        rt.attempts.update(attempts)
        from coscientist.cli.errors import CliRefuse

        with pytest.raises(CliRefuse, match="study_id"):
            await record_hypotheses(rt, aid, _hyp_out())
        assert rt.graph.nodes_by_kind("hypothesis") == []
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_links_question_ref_addresses_edges(tmp_path) -> None:
    sid = "st_hypv2_edges"
    rt = await _runtime_with_hyp_recorded(tmp_path, sid)
    try:
        aid = await _aid_for(rt, "HYP")
        await record_hypotheses(rt, aid, _hyp_out())
        hyp_task = TaskSpec(
            task_key="HYP-0", title="hypotheses", goal="propose", route="hypotheses"
        )
        fe = TaskSpec(
            task_key="FE",
            title="fe",
            goal="fe",
            route="experiment",
            depends_on=["HYP"],
            foreach=ForEachSpec(kind="hypothesis", of_task="HYP", selector="selected"),
            criteria=[],
            addresses=[],
        )
        pid = await rt.accept_plan(
            [hyp_task, fe],
            questions=[QuestionSpec(id="q1", text="does the model interpolate?")],
            hypotheses=_NEED_ON,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid
        edges = [e for e in rt.graph.all_edges() if e.kind == "addresses"]
        assert len(edges) == 3, f"expected 3 addresses edges, got {edges}"
        assert all(e.dst_id.endswith(":question:q1") for e in edges)
        assert all(e.src_id.startswith(f"{sid}:hypothesis:") for e in edges)
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_unmatched_question_ref_stays_metadata(tmp_path) -> None:
    """question_ref matching no plan question: no invented edge, attr preserved."""
    sid = "st_hypv2_unmatched"
    rt = await _runtime_with_hyp_recorded(tmp_path, sid)
    try:
        aid = await _aid_for(rt, "HYP")
        await record_hypotheses(rt, aid, _hyp_out(question_ref="q9"))
        hyp_task = TaskSpec(
            task_key="HYP-0", title="hypotheses", goal="propose", route="hypotheses"
        )
        pid = await rt.accept_plan(
            [hyp_task],
            questions=[QuestionSpec(id="q1", text="other question")],
            hypotheses=_NEED_ON,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid
        assert [e for e in rt.graph.all_edges() if e.kind == "addresses"] == []
        nodes = rt.graph.nodes_by_kind("hypothesis")
        assert {n.attrs["question_ref"] for n in nodes} == {"q9"}
    finally:
        await close_runtime(rt)


class _FoldTarget(LocalProjectMixin):
    def __init__(self) -> None:
        self.graph = GraphStore(default_execution_registry())
        self.verdict_snapshots: dict[str, dict] = {}
        self.hypothesis_verdicts: dict[str, dict] = {}


def _upsert_hypothesis(target: _FoldTarget, sid: str, hid: str) -> None:
    from coscientist.core.claims.graph import GraphNode

    target.graph.upsert_node(
        GraphNode(
            node_id=f"{sid}:hypothesis:{hid}",
            kind="hypothesis",
            study_id=sid,
            ref_id=hid,
            attrs={"statement": "s", "prediction": "p", "selected": True, "rank": 0},
        )
    )


def test_fold_stamps_verdict_onto_hypothesis_nodes() -> None:
    t = _FoldTarget()
    sid = "st_fold"
    _upsert_hypothesis(t, sid, "H_sup")
    _upsert_hypothesis(t, sid, "H_ref")
    _upsert_hypothesis(t, sid, "H_un")
    t._fold_finalized_graph(
        sid,
        {
            "question_dispositions": {},
            "hypothesis_verdicts": {
                "H_sup": {"verdict": "supported", "rule": "support_min"},
                "H_ref": {"verdict": "refuted", "rule": "k_ref"},
                "H_un": {"verdict": "untested", "rule": "no_counted"},
                "H_ghost": {"verdict": "refuted", "rule": "k_ref"},
            },
            "terminal_reason": "completed",
        },
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    nodes = {str(n.ref_id): n for n in t.graph.nodes_by_kind("hypothesis")}
    assert nodes["H_sup"].attrs["verdict"] == "supported"
    # refuted is a valid scientific result and lands in the graph/report as-is.
    assert nodes["H_ref"].attrs["verdict"] == "refuted"
    assert nodes["H_un"].attrs["verdict"] == "untested"
    assert "H_ghost" not in nodes  # no node invented for the verdict row
