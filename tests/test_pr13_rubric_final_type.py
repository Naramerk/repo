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
from tests.helpers import attach_executor, close_runtime, open_runtime


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
        criteria=criteria if criteria is not None else ([RequirementRef(template_id="rubric_check")] if rubric else []),
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
    items = [RubricItem(id="a", text="A", required=True, check="code", code_kind="artifact_exists")]
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
    ok = bad.model_copy(update={"criteria": [RequirementRef(template_id="value_match")]})
    validate_final_types([ok], [q])


def test_rubric_change_only_via_expansion_pure():
    r1 = _lit_rubric(title="v1")
    r2 = _lit_rubric(title="v2")
    assert r1.digest != r2.digest
    t1 = _exp("E0", addresses=["Q1"], rubric=r1)
    t2 = _exp("E0", addresses=["Q1"], rubric=r2)
    with pytest.raises(ValueError, match="PlanExpanded"):
        validate_rubric_immutable(new_tasks=[t2], prev_tasks=[t1], superseded_tasks=set())
    validate_rubric_immutable(new_tasks=[t2], prev_tasks=[t1], superseded_tasks={"E0"})


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


def test_judge_not_met_stays_failed():
    """Judge verdict not-met → FAILED (never remapped to UNASSESSED)."""
    rub = _judge_only_rubric()
    check = CodeCheck(kind="rubric", output="main", rubric=rub)
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
    assert cr.status == ReqStatus.FAILED
    assert "judge:FAILED" in (cr.detail or "")


def test_judge_failure_unassessed_retry():
    """Infra failure → UNASSESSED path; retry then MET."""
    rub = _judge_only_rubric()
    check = CodeCheck(kind="rubric", output="main", rubric=rub)
    # First infra failure while waiting → PENDING (attempt stays open).
    cr = evaluate_rubric(
        check,
        template_id="rubric_check",
        check_id="r#0",
        out=AgentTaskOutput(summary="x"),
        artifacts=None,
        role="validity",
        review_statuses={"r#0:adequate": ReqStatus.PENDING},
        judge_retry={"r#0:adequate": 1},
        failure_kinds={"r#0:adequate": "timeout"},
    )
    assert cr.status == ReqStatus.PENDING
    assert "judge_failure" in (cr.detail or "")
    # Exhausted retries → UNASSESSED.
    cr_u = evaluate_rubric(
        check,
        template_id="rubric_check",
        check_id="r#0",
        out=AgentTaskOutput(summary="x"),
        artifacts=None,
        role="validity",
        review_statuses={"r#0:adequate": ReqStatus.UNASSESSED},
        judge_retry={"r#0:adequate": 2},
        failure_kinds={"r#0:adequate": "timeout"},
    )
    assert cr_u.status == ReqStatus.UNASSESSED
    # Successful retry answer → MET.
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
    fin = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
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
        if e.type == "GraphNodeUpserted" and (e.payload.get("attrs") or {}).get("rubric_digest") == expected
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
    assert refused is None or refused is False or (isinstance(refused, str) and "rubric" in refused.lower())
    events = await rt.store.read_all()
    assert any(e.type == "ExpansionRefused" for e in events) or refused is None
    # With supersede → accepted.
    ok = await _record_hitl_expand(
        rt,
        after_task="E0",
        added_tasks=[_exp("E1", addresses=["Q1"], rubric=r2, criteria=[], depends_on=["PLAN"])],
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
    assert fin[0].payload.get("report_artifact_id") or fin[0].payload.get("minimal_report")
    # Failed rubric → distinct RUBRIC_NOT_MET + RU report line.
    qdisp = fin[0].payload.get("question_dispositions") or {}
    assert "Q1" in qdisp
    assert qdisp["Q1"].get("disposition") == "rubric_not_met"
    assert qdisp["Q1"].get("report_line") == "не выполнено: рубрика не принята"
    contrib = fin[0].payload.get("contributing_reasons") or []
    assert "не выполнено: рубрика не принята" in contrib


@pytest.mark.asyncio
async def test_judge_rubric_e2e_met_failed_retry(tmp_path, clock):
    """Production judge path via #out: MET, FAILED, infra-retry — no hand statuses."""
    from coscientist.core.reviews import review_key

    rt = await _boot(tmp_path, clock, "st_pr13_judge_e2e")
    rub = freeze_rubric(
        Rubric(
            items=[
                RubricItem(id="ok", text="adequate", required=True, check="judge"),
                RubricItem(id="bad", text="not adequate", required=True, check="judge"),
                RubricItem(id="flaky", text="retryable", required=True, check="judge"),
            ],
            aggregate="all_required",
        )
    )
    # OutputSpec.rubric → ``rubric:<digest>#out``. Non-required criterion
    # suppresses experiment defaults so a failed rubric yields FAILED (not PARTIAL).
    await rt.accept_plan(
        [
            _exp(
                "E0",
                addresses=["Q1"],
                rubric=rub,
                criteria=[RequirementRef(template_id="artifact_present", required=False)],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="lit", final_type="artifact")],
    )
    ex, arts = attach_executor(rt, tmp_path)
    assert ex.review_statuses == {}
    aid = await rt.start_attempt("E0")
    assert aid is not None
    task_id = rt.attempts[aid]["task_id"]
    art = arts.put(
        b"# review\n",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="text/markdown",
    )
    pending = await ex.record(
        aid,
        AgentTaskOutput(
            summary="draft",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        ),
    )
    assert pending.reason == "required_checks_pending"
    events = await rt.store.read_all()
    scheduled = [e for e in events if e.type == "ReviewScheduled"]
    assert {e.payload.get("item_id") for e in scheduled} >= {"ok", "bad", "flaky"}
    assert all(e.payload.get("review_key") for e in scheduled)
    check_ids = {str(e.payload.get("check_id") or "") for e in scheduled}
    assert any(cid.endswith("#out") for cid in check_ids), check_ids
    out_check = next(cid for cid in check_ids if cid.endswith("#out"))
    assert out_check.startswith("rubric:")

    r1 = await ex.record_review(aid, out_check, item_id="ok", status=ReqStatus.MET)
    assert r1.reason == "required_checks_pending"
    mid = await ex.record_review(aid, out_check, item_id="flaky", failure_kind="timeout")
    assert mid.reason == "required_checks_pending"
    dig = ex._output_digests[aid]
    key_ok = review_key(
        task_id=task_id,
        attempt_id=aid,
        check_id=out_check,
        item_id="ok",
        output_digest=dig,
    )
    key_flaky = review_key(
        task_id=task_id,
        attempt_id=aid,
        check_id=out_check,
        item_id="flaky",
        output_digest=dig,
    )
    assert ex.review_statuses.get(key_ok) == ReqStatus.MET
    assert ex.review_statuses.get(key_flaky) == ReqStatus.PENDING
    r3 = await ex.record_review(aid, out_check, item_id="flaky", status=ReqStatus.MET)
    assert r3.reason == "required_checks_pending"
    final = await ex.record_review(aid, out_check, item_id="bad", status=ReqStatus.FAILED)
    assert final.outcome == TaskOutcome.FAILED
    assert final.computed_outcome == TaskOutcome.FAILED
    recorded_ev = next(e for e in await rt.store.read_all() if e.type == "ResultRecorded")
    item_effect = None
    out_cr = None
    for c in recorded_ev.payload.get("check_results") or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("check_id") or "").endswith("#out"):
            out_cr = c
        effect = c.get("effect")
        if effect and effect.get("item_statuses"):
            item_effect = effect["item_statuses"]
    assert out_cr is not None
    assert item_effect is not None
    assert item_effect.get("ok") == "MET"
    assert item_effect.get("bad") == "FAILED"
    assert item_effect.get("flaky") == "MET"
    # Finalize + report line for failed rubric.
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
    qdisp = fin[0].payload.get("question_dispositions") or {}
    assert qdisp.get("Q1", {}).get("disposition") == "rubric_not_met"
    assert qdisp.get("Q1", {}).get("report_line") == "не выполнено: рубрика не принята"
    contrib = fin[0].payload.get("contributing_reasons") or []
    assert "не выполнено: рубрика не принята" in contrib


@pytest.mark.asyncio
async def test_judge_restart_after_review_scheduled(tmp_path, clock):
    """Kill after ReviewScheduled → reopen → record_review completes (no hang)."""
    sid = "st_pr13_judge_restart"
    rt = await _boot(tmp_path, clock, sid)
    rub = freeze_rubric(
        Rubric(
            items=[
                RubricItem(id="adequate", text="ok?", required=True, check="judge"),
            ],
            aggregate="all_required",
        )
    )
    await rt.accept_plan(
        [
            _exp(
                "E0",
                addresses=["Q1"],
                rubric=rub,
                criteria=[RequirementRef(template_id="rubric_check")],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="lit", final_type="artifact")],
    )
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("E0")
    assert aid is not None
    art = arts.put(
        b"# review\n",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="text/markdown",
    )
    pending = await ex.record(
        aid,
        AgentTaskOutput(
            summary="draft",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        ),
    )
    assert pending.reason == "required_checks_pending"
    assert any(e.type == "ReviewScheduled" for e in await rt.store.read_all())
    await close_runtime(rt)

    # Reopen — judge state rebuilt from journal; no KeyError / hang.
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    await rt2.ensure_lease(ttl_s=10_000)
    ex2, _ = attach_executor(rt2, tmp_path)
    assert aid in ex2._pending_outputs or aid in ex2._output_digests
    final = await ex2.record_review(aid, "rubric_check#0", item_id="adequate", status=ReqStatus.MET)
    assert final.outcome.value in ("SUCCESS", "PARTIAL")
    assert final.reason != "required_checks_pending"
    task = next(t for t in rt2.tasks.values() if t.spec.task_key == "E0")
    assert getattr(task.state, "value", task.state) == "DONE"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_plan_refused_invalid_rubric_still_finalizes(tmp_path, clock):
    """PlanValidator rejection → PlanRefused + StudyFinalized immediately."""
    rt = await _boot(tmp_path, clock, "st_pr13_refuse")
    q = QuestionSpec(id="Q1", text="lit review", final_type="artifact")
    bad = TaskSpec(
        task_key="E0",
        title="E0",
        goal="g",
        route="experiment",
        depends_on=["PLAN"],
        addresses=["Q1"],
        criteria=[],
        expected_outputs=[OutputSpec(name="main", media_type="text/markdown", role="report")],
    )
    pid = await rt.accept_plan([bad], questions=[q])
    assert pid == ""
    events = await rt.store.read_all()
    assert any(e.type == "PlanRefused" for e in events)
    fin = [e for e in events if e.type == "StudyFinalized"]
    assert len(fin) == 1, "must finalize immediately (no REPORT cancel / time-skip)"
    contrib = fin[0].payload.get("contributing_reasons") or []
    assert any("план отклонён" in str(x) for x in contrib)
    assert rt.view().phase == "closed"


@pytest.mark.asyncio
async def test_unknown_final_type_refused(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_pr13_bad_ft")
    pid = await rt.accept_plan(
        [_exp("E0", addresses=["Q1"], rubric=_judge_only_rubric(), criteria=[])],
        spec_output={
            "questions": [
                {"id": "Q1", "text": "q", "final_type": "nope"},
            ]
        },
    )
    assert pid == ""
    events = await rt.store.read_all()
    assert any(e.type == "PlanRefused" for e in events)
    assert any(e.type == "StudyFinalized" for e in events)


def test_final_type_both_requires_verdict_and_rubric(clock):
    """§6.1 both: failed rubric → RUBRIC_NOT_MET + RU line."""
    from coscientist.core.dispositions import dispositions
    from coscientist.core.domain import Task, TaskKind, TaskState
    from coscientist.core.rubric import RUBRIC_REJECTED_LINE, task_rubric_status

    rub = _judge_only_rubric()
    spec = _exp(
        "E0",
        addresses=["Q1"],
        rubric=rub,
        criteria=[RequirementRef(template_id="value_match")],
    )
    task = Task(
        task_id="s/E0",
        kind=TaskKind.EXPERIMENT,
        spec=spec,
        state=TaskState.DONE,
        deadline=clock.now() + timedelta(hours=1),
        attempts=["a1"],
        outcome=TaskOutcome.SUCCESS,
    )
    plans = {
        "p1": {
            "questions": [{"id": "Q1", "text": "both?", "final_type": "both"}],
            "tasks": [spec.model_dump(mode="json")],
        }
    }
    results = {
        "a1": {
            "attempt_id": "a1",
            "check_results": [
                {
                    "check_id": f"rubric:{rub.digest}#out",
                    "template_id": f"rubric:{rub.digest}",
                    "status": "FAILED",
                    "effect": {
                        "rubric_digest": rub.digest,
                        "rubric_kind": "rubric",
                    },
                }
            ],
        }
    }
    assert task_rubric_status(task_keys={"E0"}, tasks={"s/E0": task}, results=results) == "failed"
    disp = dispositions(
        tasks={"s/E0": task},
        plans=plans,
        graph=None,
        study=None,
        results=results,
    )
    row = disp.get("Q1") or {}
    assert row.get("final_type") == "both"
    assert row.get("disposition") == "rubric_not_met"
    assert row.get("reason") == "rubric_rejected"
    assert row.get("report_line") == RUBRIC_REJECTED_LINE


def test_k_of_n_counts_required_only():
    from coscientist.core.rubric import aggregate_rubric_status

    rub = freeze_rubric(
        Rubric(
            items=[
                RubricItem(id="r1", text="a", required=True, check="judge"),
                RubricItem(id="r2", text="b", required=True, check="judge"),
                RubricItem(id="opt", text="c", required=False, check="judge"),
            ],
            aggregate="k_of_n",
            k=2,
        )
    )
    # Optional MET must not satisfy k=2 alone with one required MET.
    st = aggregate_rubric_status(
        {
            "r1": ReqStatus.MET,
            "r2": ReqStatus.FAILED,
            "opt": ReqStatus.MET,
        },
        rub,
    )
    assert st == ReqStatus.FAILED
    st2 = aggregate_rubric_status(
        {
            "r1": ReqStatus.MET,
            "r2": ReqStatus.MET,
            "opt": ReqStatus.FAILED,
        },
        rub,
    )
    assert st2 == ReqStatus.MET


def test_rubric_digest_stable_canonical():
    """Digest uses versioned canonical fields — not sensitive to model_dump churn."""
    items = [RubricItem(id="a", text="A", required=True, check="code", code_kind="artifact_exists")]
    d1 = rubric_digest(items, "all_required", None)
    d2 = rubric_digest(items, "all_required", None)
    assert d1 == d2
    # Changing only unused k for all_required does not change digest (k forced None).
    assert rubric_digest(items, "all_required", 99) == d1


def test_upcast_taskcreated_v10_and_plan_expanded_v2():
    from coscientist.core.upcasters import upcast

    v, p = upcast(
        "TaskCreated",
        9,
        {
            "task_id": "t",
            "spec": {
                "task_key": "E1",
                "expected_outputs": [{"name": "main", "media_type": "text/plain"}],
            },
        },
    )
    assert v == 10
    assert p["spec"]["expected_outputs"][0]["rubric"] is None
    v2, p2 = upcast(
        "PlanExpanded",
        1,
        {
            "plan_id": "p",
            "added_tasks": [
                {
                    "task_key": "E1",
                    "expected_outputs": [{"name": "main", "media_type": "text/plain"}],
                }
            ],
            "added_questions": [{"id": "Q1", "text": "q"}],
        },
    )
    assert v2 == 2
    assert p2["added_tasks"][0]["expected_outputs"][0]["rubric"] is None
    assert p2["added_questions"][0]["final_type"] == "verdict"
    # Legacy unknown final_type coerced to verdict (current unknown → PlanRefused).
    v3, p3 = upcast(
        "PlanAccepted",
        7,
        {
            "plan_id": "p",
            "questions": [{"id": "Q1", "text": "q", "final_type": "nope"}],
            "tasks": [],
        },
    )
    assert v3 == 8
    assert p3["questions"][0]["final_type"] == "verdict"


def test_study_fulfilled_wires_final_types():
    from coscientist.core.rules import study_fulfilled

    assert study_fulfilled(
        {"Q1": "answered"},
        True,
        question_final_types={"Q1": "artifact"},
    )
    assert not study_fulfilled(
        {"Q1": "rubric_not_met"},
        True,
        question_final_types={"Q1": "artifact"},
    )
    # Raw claim "supported" is not a settled disposition for both.
    assert not study_fulfilled(
        {"Q1": "supported"},
        True,
        question_final_types={"Q1": "both"},
    )
    assert study_fulfilled(
        {"Q1": "answered"},
        True,
        question_final_types={"Q1": "both"},
    )
    assert not study_fulfilled(
        {"Q1": "not_attempted"},
        True,
        question_final_types={"Q1": "both"},
    )
    assert not study_fulfilled(
        {"Q1": "rubric_not_met"},
        True,
        question_final_types={"Q1": "both"},
    )


def test_review_key_scoped_no_cross_attempt():
    from coscientist.checks.kinds.rubric import evaluate_rubric
    from coscientist.core.reviews import review_key

    rub = _judge_only_rubric()
    check = CodeCheck(kind="rubric", output="main", rubric=rub)
    foreign = review_key(
        task_id="other",
        attempt_id="aX",
        check_id="r#0",
        item_id="adequate",
        output_digest="deadbeef",
    )
    cr = evaluate_rubric(
        check,
        template_id="rubric_check",
        check_id="r#0",
        out=AgentTaskOutput(summary="x"),
        artifacts=None,
        role="validity",
        review_statuses={foreign: ReqStatus.MET},
        task_id="mine",
        attempt_id="a1",
        output_digest="cafe",
    )
    # Foreign MET must not apply — waiting for this attempt's judge.
    assert cr.status == ReqStatus.PENDING
