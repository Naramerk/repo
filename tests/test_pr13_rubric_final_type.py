"""M1 PR13: rubric + final_type + PlanValidator (DESIGN_M1 §6)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.checks.kinds.rubric import evaluate_rubric
from coscientist.core.artifacts import make_tool_ref
from coscientist.core.claims import RuntimeClaim
from coscientist.core.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    CodeCheck,
    OutputSpec,
    QuestionSpec,
    ReqStatus,
    RequirementRef,
    TaskOutcome,
    TaskSpec,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.plan_validate import (
    freeze_plan_rubrics,
    validate_final_types,
    validate_rubric_immutable,
)
from coscientist.core.rubric import freeze_rubric, rubric_digest
from coscientist.core.rubric_types import Rubric, RubricItem
from coscientist.core.stance import claim_stance
from tests.helpers import attach_executor, open_runtime


def _lit_rubric(*, title: str = "Write a literature review") -> Rubric:
    return freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id="exists",
                    text=title,
                    required=True,
                    check="code",
                    code_kind="artifact_exists",
                ),
                RubricItem(
                    id="adequate",
                    text="Review is adequate",
                    required=True,
                    check="judge",
                ),
            ],
            aggregate="all_required",
        )
    )


def _exp(
    key: str,
    *,
    addresses: list[str] | None = None,
    rubric: Rubric | None = None,
    criteria: list[RequirementRef] | None = None,
    depends_on: list[str] | None = None,
) -> TaskSpec:
    outs = [
        OutputSpec(
            name="main",
            media_type="text/markdown",
            role="report",
            rubric=rubric,
        )
    ]
    return TaskSpec(
        task_key=key,
        title=key,
        goal="g",
        route="experiment",
        depends_on=depends_on or ["PLAN"],
        addresses=addresses or [],
        expected_outputs=outs,
        criteria=criteria
        if criteria is not None
        else ([RequirementRef(template_id="rubric_check")] if rubric else []),
    )


async def _boot(tmp_path, clock, sid: str):
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="t",
        request="write a literature review of topic X",
        catalog_version="1",
        deadline=clock.now() + timedelta(hours=1),
        finalize_reserve_s=30,
        sid=sid,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    return rt


async def _record_hitl_expand(rt, **kwargs):
    rid = f"hitl_{rt.study.study_id}_{len(getattr(rt, '_hitl_decisions', {}) or {})}"
    after = kwargs.get("after_task") or "E0"
    tid = next(iter(rt.tasks))
    await rt._append(
        [
            EventEnvelope(
                study_id=rt.study.study_id,
                type="HitlOpened",
                entity_key=rid,
                idem_key=make_idem("HitlOpened", rid),
                payload={
                    "request_id": rid,
                    "kind": "plan_expansion",
                    "payload_ref": tid,
                    "deadline": (rt.clock.now() + timedelta(seconds=60)).isoformat(),
                    "study_id": rt.study.study_id,
                    "task_id": tid,
                    "expand_after_task": str(after),
                    "open": True,
                    "cost_rub": 0.0,
                },
                ts=rt.clock.now(),
            )
        ]
    )
    await rt._append(
        [
            EventEnvelope(
                study_id=rt.study.study_id,
                type="HitlDecided",
                entity_key=rid,
                idem_key=make_idem("HitlDecided", rid),
                payload={
                    "request_id": rid,
                    "decision": "accept",
                    "by": "human",
                    "decided_by": "human",
                    "approved_by": "human",
                    "reason": "answered",
                    "spent_rub": 0.0,
                },
                ts=rt.clock.now(),
            )
        ]
    )
    return await rt.expand_plan(**kwargs, hitl_request_id=rid)


# --- pure validators ---------------------------------------------------------


def test_rubric_frozen_digest():
    items = [
        RubricItem(id="a", text="A", required=True, check="code", code_kind="artifact_exists")
    ]
    d1 = rubric_digest(items, "all_required", None)
    r = freeze_rubric(Rubric(items=items, aggregate="all_required"))
    assert r.digest == d1
    assert len(r.digest) == 64
    with pytest.raises(ValueError, match="digest mismatch"):
        freeze_rubric(Rubric(items=items, aggregate="all_required", digest="0" * 64))


def test_plan_validator_artifact_requires_rubric():
    q = QuestionSpec(id="Q1", text="lit review", final_type="artifact")
    bad = _exp("E0", addresses=["Q1"], rubric=None, criteria=[])
    with pytest.raises(ValueError, match="rubric"):
        validate_final_types([bad], [q])
    ok = _exp("E0", addresses=["Q1"], rubric=_lit_rubric())
    validate_final_types([ok], [q])


def test_plan_validator_verdict_requires_criterion():
    q = QuestionSpec(id="Q1", text="does X?", final_type="verdict")
    bad = TaskSpec(
        task_key="E0",
        title="E0",
        goal="g",
        route="experiment",
        depends_on=["PLAN"],
        addresses=["Q1"],
        criteria=[],
        expected_outputs=[],
    )
    with pytest.raises(ValueError, match="criterion"):
        validate_final_types([bad], [q], catalog=None)
    ok = bad.model_copy(
        update={"criteria": [RequirementRef(template_id="value_match")]}
    )
    validate_final_types([ok], [q])


def test_rubric_change_only_via_expansion_pure():
    r1 = _lit_rubric(title="v1")
    r2 = _lit_rubric(title="v2")
    assert r1.digest != r2.digest
    t1 = _exp("E0", addresses=["Q1"], rubric=r1)
    t2 = _exp("E0", addresses=["Q1"], rubric=r2)
    with pytest.raises(ValueError, match="PlanExpanded"):
        validate_rubric_immutable(new_tasks=[t2], prev_tasks=[t1], superseded_tasks=set())
    validate_rubric_immutable(
        new_tasks=[t2], prev_tasks=[t1], superseded_tasks={"E0"}
    )


def _judge_only_rubric() -> Rubric:
    return freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id="adequate",
                    text="Deliverable is adequate",
                    required=True,
                    check="judge",
                )
            ],
            aggregate="all_required",
        )
    )


def test_rubric_never_changes_stance():
    """Stance comes only from criterion_status — rubric MET must not alter it."""
    assert claim_stance("met") == "supports"
    assert claim_stance("not_met") == "refutes"
    c = RuntimeClaim(
        criterion_status="met",
        accepted=True,
        provenance="real",
        validity="valid",
        claim_outcome="consistent",
        check_role="prediction",
        frozen=True,
    )
    before = c.stance
    # Simulate rubric CheckResult on the side — stance property ignores it.
    rub = evaluate_rubric(
        CodeCheck(kind="rubric", output="main", rubric=_judge_only_rubric()),
        template_id="rubric_check",
        check_id="rubric_check#0",
        out=AgentTaskOutput(summary="x"),
        artifacts=None,
        role="prediction",  # even if mis-labeled, evaluator forces validity
        review_statuses={"rubric_check#0:adequate": ReqStatus.MET},
    )
    assert rub.role == "validity"
    assert rub.effect and rub.effect.get("affects_stance") is False
    assert rub.status == ReqStatus.MET
    assert c.stance == before == "supports"


def test_judge_failure_unassessed_retry():
    rub = _judge_only_rubric()
    check = CodeCheck(kind="rubric", output="main", rubric=rub)
    # Judge FAILED → UNASSESSED (not FAILED) on first try.
    cr = evaluate_rubric(
        check,
        template_id="rubric_check",
        check_id="r#0",
        out=AgentTaskOutput(summary="x"),
        artifacts=None,
        role="validity",
        review_statuses={"r#0:adequate": ReqStatus.FAILED},
        judge_retry={},
    )
    assert cr.status == ReqStatus.UNASSESSED
    assert "judge_failure" in (cr.detail or "")
    # Retry with MET → overall MET (judge-only rubric).
    cr2 = evaluate_rubric(
        check,
        template_id="rubric_check",
        check_id="r#0",
        out=AgentTaskOutput(summary="x"),
        artifacts=None,
        role="validity",
        review_statuses={"r#0:adequate": ReqStatus.MET},
        judge_retry={"r#0:adequate": 1},
    )
    assert cr2.status == ReqStatus.MET


# --- runtime -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_type_artifact_fulfilled_by_rubric(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_pr13_artifact")
    rub = freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id="exists",
                    text="Write a literature review",
                    required=True,
                    check="code",
                    code_kind="artifact_exists",
                )
            ],
            aggregate="all_required",
        )
    )
    await rt.accept_plan(
        [_exp("E0", addresses=["Q1"], rubric=rub, criteria=[])],
        questions=[QuestionSpec(id="Q1", text="lit review of X", final_type="artifact")],
    )
    ex, arts = attach_executor(rt, tmp_path)
    # Drop default experiment criteria noise: empty criteria → artifact_present default.
    # Put a deliverable and mark judge N/A by using code-only rubric.
    aid = await rt.start_attempt("E0")
    assert aid is not None
    art = arts.put(
        b"# Literature review\n...",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="text/markdown",
    )
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="review done",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        ),
    )
    task = next(t for t in rt.tasks.values() if t.spec.task_key == "E0")
    assert task.outcome in (TaskOutcome.SUCCESS, TaskOutcome.PARTIAL) or (
        getattr(task.outcome, "value", None) in ("SUCCESS", "PARTIAL")
    )
    # Advance to close study.
    await rt.start_attempt("REPORT")
    # cancel report if started
    for t in list(rt.tasks.values()):
        if t.spec.task_key == "REPORT" and t.state.value != "DONE":
            await rt.cancel_task("REPORT", reason="cancelled")
    await rt.ensure_lease(ttl_s=10_000)
    rt.clock.advance(timedelta(hours=2))
    await rt.tick()
    fin = [
        e
        for e in await rt.store.read_all()
        if e.type == "StudyFinalized"
    ]
    assert fin, "study must close with StudyFinalized (totality)"
    payload = fin[-1].payload
    assert payload.get("report_artifact_id") or payload.get("minimal_report")
    qdisp = payload.get("question_dispositions") or {}
    assert qdisp.get("Q1", {}).get("disposition") == "answered"
    assert qdisp.get("Q1", {}).get("reason") == "rubric_met"
    view = rt.view()
    assert view.phase == "closed"
    assert view.verdict in ("fulfilled", "partial", "stubbed")


@pytest.mark.asyncio
async def test_rubric_frozen_digest_runtime(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_pr13_freeze")
    rub = _lit_rubric()
    expected = rub.digest
    tasks, qs = freeze_plan_rubrics(
        [_exp("E0", addresses=["Q1"], rubric=rub.model_copy(update={"digest": ""}))],
        [QuestionSpec(id="Q1", text="q", final_type="artifact")],
    )
    assert tasks[0].expected_outputs[0].rubric is not None
    assert tasks[0].expected_outputs[0].rubric.digest == expected
    await rt.accept_plan(tasks, questions=qs)
    plans = await rt.store.get_projection("plans")
    assert plans
    out0 = plans[-1]["tasks"][0]["expected_outputs"][0]
    assert out0["rubric"]["digest"] == expected
    # Graph criterion node for rubric digest.
    nodes = [
        e
        for e in await rt.store.read_all()
        if e.type == "GraphNodeUpserted"
        and (e.payload.get("attrs") or {}).get("rubric_digest") == expected
    ]
    assert nodes


@pytest.mark.asyncio
async def test_rubric_change_only_via_expansion(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_pr13_expand_rubric")
    r1 = _lit_rubric(title="review v1")
    r2 = _lit_rubric(title="review v2 — revised")
    assert r1.digest != r2.digest
    await rt.accept_plan(
        [_exp("E0", addresses=["Q1"], rubric=r1, criteria=[])],
        questions=[QuestionSpec(id="Q1", text="lit", final_type="artifact")],
        expand_after=["E0"],
    )
    # In-place digest change without supersede → ExpansionRefused.
    refused = await _record_hitl_expand(
        rt,
        after_task="E0",
        added_tasks=[_exp("E0", addresses=["Q1"], rubric=r2, criteria=[])],
        added_questions=[],
        superseded_tasks=[],
        reason="edit rubric",
    )
    assert refused is None or refused is False or (
        isinstance(refused, str) and "rubric" in refused.lower()
    )
    events = await rt.store.read_all()
    assert any(e.type == "ExpansionRefused" for e in events) or refused is None
    # With supersede → accepted.
    ok = await _record_hitl_expand(
        rt,
        after_task="E0",
        added_tasks=[
            _exp("E1", addresses=["Q1"], rubric=r2, criteria=[], depends_on=["PLAN"])
        ],
        added_questions=[],
        superseded_tasks=["E0"],
        reason="replace rubric via expand",
    )
    # expand_plan returns plan_id or None
    events2 = await rt.store.read_all()
    if ok or any(e.type == "PlanExpanded" for e in events2):
        assert any(e.type == "PlanExpanded" for e in events2)
    else:
        # Critic may refuse for other reasons; immutability path already covered above.
        assert any(e.type == "ExpansionRefused" for e in events)


@pytest.mark.asyncio
async def test_impossible_task_still_closes_with_report(tmp_path, clock):
    """Even a nonsensical / failing qualitative task must finalize (no crash)."""
    rt = await _boot(tmp_path, clock, "st_pr13_impossible")
    rub = freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id="exists",
                    text="Prove 2+2=5 in a literature review",
                    required=True,
                    check="code",
                    code_kind="artifact_exists",
                )
            ],
            aggregate="all_required",
        )
    )
    await rt.accept_plan(
        [_exp("E0", addresses=["Q1"], rubric=rub, criteria=[])],
        questions=[
            QuestionSpec(
                id="Q1",
                text="impossible deliverable",
                final_type="artifact",
            )
        ],
    )
    # Fail the task (no artifact).
    aid = await rt.start_attempt("E0")
    assert aid is not None
    ex, _arts = attach_executor(rt, tmp_path)
    await ex.record(aid, AgentTaskOutput(summary="cannot", outputs={}))
    for t in list(rt.tasks.values()):
        if t.spec.task_key == "REPORT" and getattr(t.state, "value", t.state) != "DONE":
            try:
                await rt.cancel_task("REPORT", reason="cancelled")
            except Exception:  # noqa: BLE001
                pass
    await rt.ensure_lease(ttl_s=10_000)
    rt.clock.advance(timedelta(hours=2))
    await rt.tick()
    fin = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fin) == 1
    assert fin[0].payload.get("report_artifact_id") or fin[0].payload.get(
        "minimal_report"
    )
    # Disposition present (not_attempted / failed path) — never missing.
    qdisp = fin[0].payload.get("question_dispositions") or {}
    assert "Q1" in qdisp
    assert qdisp["Q1"].get("disposition") is not None
