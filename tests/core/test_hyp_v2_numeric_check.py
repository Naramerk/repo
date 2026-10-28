"""HYP v2: numeric prediction criteria via EXISTING check kinds (no new engine).

Legacy CC1–CC4 shape (AD-thresholds etc.) flows through the standard criterion
machinery: a numeric catalog criterion (``metric_threshold`` on the artifact) →
claim ``checked_against`` that criterion → claim outcome (consistent /
inconsistent) → the real claims-engine verdict on the registered hypothesis
node. The preregistered ``CheckProposal(target_kind="hypothesis")`` route reuses
the same evaluation core (``_evaluate_proposed_check``) and is PR20-owned.
Insufficient evidence stays ``untested`` (no_counted) — the honest
"unverifiable"; nothing is invented.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coscientist.cli.journal import record_hypotheses
from coscientist.core.claims.claims import hypothesis_verdicts
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    Need,
    OutputSpec,
    QuestionSpec,
    RequirementRef,
    TaskSpec,
)
from tests.core.test_hyp_v2_registration import _hyp_out
from tests.helpers import NEED_OFF, attach_executor, close_runtime, open_runtime
from tests.test_pr20_proposed_check import _create, _task_id

_NEED_ON = Need(kind="need", needed=True, reason="hypotheses required")

# Catalog numeric criterion: metric_threshold metric="effect", op=">", sesoi=0.5
# (minimal_test_catalog) — a preregistered-by-catalog numeric threshold, the
# same kind a legacy CC-threshold (AD ≥ 0.6 / кратность ≤ 3×) maps to.
_NUMERIC_CRITERION = "value_match_sesoi"


async def _study_with_hypotheses(tmp_path: Path, sid: str):
    """HYP v2 nodes registered → plan with a numeric criterion on EXP-0."""
    rt = await open_runtime(tmp_path, sid)
    await _create(rt, "hyp v2 numeric")
    attach_executor(rt, tmp_path)
    await rt.start_attempt("SPEC")
    await rt.cancel_task("SPEC", reason="cancelled")
    hyp_aid = await rt.start_attempt("HYP")
    assert hyp_aid
    await record_hypotheses(rt, str(hyp_aid), _hyp_out())  # registers H0/H1/H2
    exp = TaskSpec(
        task_key="EXP-0",
        title="e",
        goal="g",
        route="experiment",
        # No depends_on: the plan's HYP-0 placeholder stays PENDING (the HYP
        # stage itself already closed SUCCESS via record_hypotheses); a dep
        # would block the evaluating attempt with no executor pass on HYP-0.
        depends_on=[],
        addresses=["q1"],
        expected_outputs=[OutputSpec(name="main", media_type="application/json")],
        criteria=[RequirementRef(template_id=_NUMERIC_CRITERION)],
    )
    hyp_plan = TaskSpec(
        task_key="HYP-0", title="hypotheses", goal="propose", route="hypotheses"
    )
    await rt.accept_plan(
        [hyp_plan, exp],
        questions=[QuestionSpec(id="q1", text="does the effect exceed the threshold?")],
        hypotheses=_NEED_ON,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )
    exp_tid = _task_id(rt, "EXP-0")
    return rt, exp_tid


async def _record_expo(rt, tmp_path: Path, task_id: str, *, effect: float) -> None:
    # provenance="real": the fake store's producer is the test agent; stub
    # provenance would (honestly) keep the claim from counting.
    aid = await rt.start_attempt(task_id, provenance="real")
    assert aid
    ex, arts = attach_executor(rt, tmp_path)
    art = arts.put(
        json.dumps({"effect": effect}).encode(),
        make_tool_ref(f"{aid}:1"),
        name="main",
        media_type="application/json",
    )
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"effect": effect},
            claims=[
                {
                    "id": "c1",
                    "target": "H0",
                    "target_kind": "hypothesis",
                    "checked_against": _NUMERIC_CRITERION,
                }
            ],
        ),
    )


def _h0_verdict(rt):
    snaps = hypothesis_verdicts(
        rt.graph,
        study_id=rt.study.study_id,
        study=rt.study,
        results=rt.results,
        k=rt.study.confirmations_needed,
        any_experiment_done=True,
    )
    return snaps["H0"]


@pytest.mark.asyncio
async def test_prediction_met_by_numeric_threshold_counts(tmp_path: Path) -> None:
    """effect 1.0 > preregistered 0.5 threshold → hypothesis counts as tested."""
    sid = "st_hypv2_num_ok"
    rt, exp_tid = await _study_with_hypotheses(tmp_path, sid)
    try:
        await _record_expo(rt, tmp_path, exp_tid, effect=1.0)
        snap = _h0_verdict(rt)
        assert snap["verdict"] in ("supported", "survived")
        assert snap["target_kind"] == "hypothesis"
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_prediction_refuted_by_numeric_threshold(tmp_path: Path) -> None:
    """effect 0.3 below the preregistered threshold → refuted (valid science)."""
    sid = "st_hypv2_num_ref"
    rt, exp_tid = await _study_with_hypotheses(tmp_path, sid)
    try:
        await _record_expo(rt, tmp_path, exp_tid, effect=0.3)
        snap = _h0_verdict(rt)
        assert snap["verdict"] == "refuted"
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_no_evidence_stays_untested_not_invented(tmp_path: Path) -> None:
    """Registered hypothesis without any claims → honest untested (no_counted)."""
    sid = "st_hypv2_num_none"
    rt, _exp_tid = await _study_with_hypotheses(tmp_path, sid)
    try:
        snap = _h0_verdict(rt)
        assert snap["verdict"] == "untested"
    finally:
        await close_runtime(rt)
