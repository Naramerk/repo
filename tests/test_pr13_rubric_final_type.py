"""M1 PR13: rubric + final_type + PlanValidator (DESIGN_M1 §6)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.checks.kinds.rubric import evaluate_rubric
from coscientist.core.claims.claims import RuntimeClaim
from coscientist.core.claims.reasons import NotAttemptedReason
from coscientist.core.claims.reviews import review_key
from coscientist.core.claims.rubric import freeze_rubric, rubric_digest
from coscientist.core.claims.rubric_types import Rubric, RubricItem
from coscientist.core.claims.stance import claim_stance
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import (
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
from coscientist.core.plan.plan_validate import (
    freeze_plan_rubrics,
    validate_final_types,
    validate_rubric_immutable,
)
from tests.helpers import TEST_NEED_KW, attach_executor, close_runtime, open_runtime, record_success

_SCOPE = dict(task_id="t1", attempt_id="a1", output_digest="deadbeef")


def _rk(check_id: str, item_id: str) -> str:
    return review_key(check_id=check_id, item_id=item_id, **_SCOPE)


async def _complete_report_and_finalize(rt, tmp_path) -> None:
    """Finish REPORT via production path (no cancel / clock skip)."""
    aid = await rt.start_attempt("REPORT")
    if aid is None:
        await rt.ensure_lease(ttl_s=10_000)
        await rt.tick()
        return
    ex, arts = attach_executor(rt, tmp_path)
    art = arts.put(
        b"# report\n",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="text/markdown",
    )
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="report",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        ),
    )
    await rt.ensure_lease(ttl_s=10_000)
    await rt.tick()


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


async def _boot(tmp_path, clock, sid: str, *, cancel_stages: bool = True):
    """Boot a study. Default cancels SPEC/HYP/LIT (fast path for non-fulfilled tests).

    Use ``cancel_stages=False`` + ``record_success`` for e2e fulfilled paths.
    """
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="t",
        request="write a literature review of topic X",
        catalog_version=3,
        deadline=clock.now() + timedelta(hours=1),
        finalize_reserve_s=30,
        sid=sid,
        require_hypotheses=False,
    )
    if cancel_stages:
        for key in ("SPEC", "HYP", "LIT"):
            await rt.start_attempt(key)
            await rt.cancel_task(key, reason="cancelled")
    return rt


async def _boot_stages_ok(tmp_path, clock, sid: str):
    """Production-path boot: SPEC/HYP/LIT succeed (required for fulfilled)."""
    rt = await _boot(tmp_path, clock, sid, cancel_stages=False)
    ex, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        aid = await rt.start_attempt(key)
        await record_success(rt, aid, arts)
    return rt, ex, arts


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
    key = _rk("rubric_check#0", "adequate")
    rub = evaluate_rubric(
        CodeCheck(
            kind="rubric",
            output="main",
            rubric=_judge_only_rubric(),
            allow_network=False,
            allow_cgroup_degraded=False,
        ),
        template_id="rubric_check",
        check_id="rubric_check#0",
        out=AgentTaskOutput(summary="x"),
        artifacts=None,
        role="prediction",  # even if mis-labeled, evaluator forces validity
        review_statuses={key: ReqStatus.MET},
        **_SCOPE,
    )
    assert rub.role == "validity"
    assert rub.effect and rub.effect.get("affects_stance") is False
    assert rub.status == ReqStatus.MET
    assert c.stance == before == "supports"


def test_judge_not_met_stays_failed():
    """Judge verdict not-met → FAILED (never remapped to UNASSESSED)."""
    rub = _judge_only_rubric()
    check = CodeCheck(kind="rubric", output="main", rubric=rub, allow_network=False, allow_cgroup_degraded=False)
    key = _rk("r#0", "adequate")
    cr = evaluate_rubric(
        check,
        template_id="rubric_check",
        check_id="r#0",
        out=AgentTaskOutput(summary="x"),
        artifacts=None,
        role="validity",
        review_statuses={key: ReqStatus.FAILED},
        judge_retry={},
        **_SCOPE,
    )
    assert cr.status == ReqStatus.FAILED
    assert "judge:FAILED" in (cr.detail or "")


def test_judge_failure_unassessed_retry():
    """Infra failure → UNASSESSED path; retry then MET."""
    rub = _judge_only_rubric()
    check = CodeCheck(kind="rubric", output="main", rubric=rub, allow_network=False, allow_cgroup_degraded=False)
    key = _rk("r#0", "adequate")
    # First infra failure while waiting → PENDING (attempt stays open).
    cr = evaluate_rubric(
        check,
        template_id="rubric_check",
        check_id="r#0",
        out=AgentTaskOutput(summary="x"),
        artifacts=None,
        role="validity",
        review_statuses={key: ReqStatus.PENDING},
        judge_retry={key: 1},
        failure_kinds={key: "timeout"},
        **_SCOPE,
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
        review_statuses={key: ReqStatus.UNASSESSED},
        judge_retry={key: 2},
        failure_kinds={key: "timeout"},
        **_SCOPE,
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
        review_statuses={key: ReqStatus.MET},
        judge_retry={key: 1},
        **_SCOPE,
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
        **TEST_NEED_KW,
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
    assert task.outcome == TaskOutcome.SUCCESS
    await _complete_report_and_finalize(rt, tmp_path)
    fin = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fin) == 1
    payload = fin[-1].payload
    assert payload.get("report_artifact_id") is not None
    qdisp = payload.get("question_dispositions") or {}
    assert qdisp.get("Q1", {}).get("disposition") == "answered"
    assert qdisp.get("Q1", {}).get("reason") == "rubric_met"
    view = rt.view()
    assert view.phase == "closed"
    assert view.verdict == payload.get("verdict")
    # Bootstrap cancels SPEC/HYP/LIT → not all required SUCCESS → partial (not raise).
    assert view.verdict == "partial"


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
    await rt.accept_plan(tasks, questions=qs, **TEST_NEED_KW)
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
        **TEST_NEED_KW,
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
    assert refused is None
    events = await rt.store.read_all()
    assert any(e.type == "ExpansionRefused" for e in events)
    # With supersede → accepted.
    ok = await _record_hitl_expand(
        rt,
        after_task="E0",
        added_tasks=[_exp("E1", addresses=["Q1"], rubric=r2, criteria=[], depends_on=["PLAN"])],
        added_questions=[],
        superseded_tasks=["E0"],
        reason="replace rubric via expand",
    )
    events2 = await rt.store.read_all()
    assert any(e.type == "PlanExpanded" for e in events2)
    assert ok is not None


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
        **TEST_NEED_KW,
    )
    # Fail the task (no artifact).
    aid = await rt.start_attempt("E0")
    assert aid is not None
    ex, _arts = attach_executor(rt, tmp_path)
    await ex.record(aid, AgentTaskOutput(summary="cannot", outputs={}))
    await _complete_report_and_finalize(rt, tmp_path)
    fin = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fin) == 1
    assert fin[0].payload.get("report_artifact_id") is not None
    # Failed rubric → distinct RUBRIC_NOT_MET + RU report line.
    qdisp = fin[0].payload.get("question_dispositions") or {}
    assert "Q1" in qdisp
    assert qdisp["Q1"].get("disposition") == "rubric_not_met"
    assert qdisp["Q1"].get("report_line") == "не выполнено: рубрика не принята"
    contrib = fin[0].payload.get("contributing_reasons") or []
    assert "не выполнено: рубрика не принята" in contrib
    assert rt.view().verdict == fin[0].payload.get("verdict")


@pytest.mark.asyncio
async def test_judge_rubric_e2e_met_failed_retry(tmp_path, clock):
    """Production judge path via #out: MET, FAILED, infra-retry — no hand statuses."""
    from coscientist.core.claims.reviews import review_key

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
        **TEST_NEED_KW,
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
    await _complete_report_and_finalize(rt, tmp_path)
    fin = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fin) == 1
    qdisp = fin[0].payload.get("question_dispositions") or {}
    assert qdisp.get("Q1", {}).get("disposition") == "rubric_not_met"
    assert qdisp.get("Q1", {}).get("report_line") == "не выполнено: рубрика не принята"
    contrib = fin[0].payload.get("contributing_reasons") or []
    assert "не выполнено: рубрика не принята" in contrib
    assert rt.view().verdict == fin[0].payload.get("verdict")


@pytest.mark.asyncio
async def test_judge_restart_after_review_scheduled(tmp_path, clock):
    """Kill after ReviewScheduled → reopen state == live; record_review completes."""
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
        **TEST_NEED_KW,
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
    live_statuses = dict(ex.review_statuses)
    live_retries = dict(ex.judge_retry)
    live_kinds = dict(ex.failure_kinds)
    live_digests = dict(ex._output_digests)
    await close_runtime(rt)

    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    await rt2.ensure_lease(ttl_s=10_000)
    ex2, _ = attach_executor(rt2, tmp_path)
    assert aid in ex2._pending_outputs
    assert aid in ex2._output_digests
    assert ex2.review_statuses == live_statuses
    assert ex2.judge_retry == live_retries
    assert ex2.failure_kinds == live_kinds
    assert ex2._output_digests == live_digests
    final = await ex2.record_review(aid, "rubric_check#0", item_id="adequate", status=ReqStatus.MET)
    assert final.outcome in (TaskOutcome.SUCCESS, TaskOutcome.PARTIAL)
    assert final.reason != "required_checks_pending"
    task = next(t for t in rt2.tasks.values() if t.spec.task_key == "E0")
    assert getattr(task.state, "value", task.state) == "DONE"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_judge_restart_after_judge_failure(tmp_path, clock):
    """Kill after judge failure (PENDING) → reopen statuses match live."""
    sid = "st_pr13_judge_restart_fail"
    rt = await _boot(tmp_path, clock, sid)
    rub = freeze_rubric(
        Rubric(
            items=[RubricItem(id="adequate", text="ok?", required=True, check="judge")],
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
        **TEST_NEED_KW,
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
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="draft",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        ),
    )
    mid = await ex.record_review(aid, "rubric_check#0", item_id="adequate", failure_kind="timeout")
    assert mid.reason == "required_checks_pending"
    # Live + journal both store PENDING (unified).
    assert any(st == ReqStatus.PENDING for st in ex.review_statuses.values())
    live_statuses = dict(ex.review_statuses)
    live_retries = dict(ex.judge_retry)
    live_kinds = dict(ex.failure_kinds)
    await close_runtime(rt)

    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    await rt2.ensure_lease(ttl_s=10_000)
    ex2, _ = attach_executor(rt2, tmp_path)
    assert ex2.review_statuses == live_statuses
    assert ex2.judge_retry == live_retries
    assert ex2.failure_kinds == live_kinds
    assert all(st == ReqStatus.PENDING for st in ex2.review_statuses.values())
    final = await ex2.record_review(aid, "rubric_check#0", item_id="adequate", status=ReqStatus.MET)
    assert final.outcome in (TaskOutcome.SUCCESS, TaskOutcome.PARTIAL)
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
    pid = await rt.accept_plan([bad], questions=[q], **TEST_NEED_KW)
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
        **TEST_NEED_KW,
    )
    assert pid == ""
    events = await rt.store.read_all()
    assert any(e.type == "PlanRefused" for e in events)
    assert any(e.type == "StudyFinalized" for e in events)


def test_final_type_both_requires_verdict_and_rubric(clock):
    """§6.1 both: failed rubric → RUBRIC_NOT_MET + RU line."""
    from coscientist.core.claims.dispositions import dispositions
    from coscientist.core.claims.rubric import RUBRIC_REJECTED_LINE, task_rubric_status
    from coscientist.core.execution.domain import Task, TaskKind, TaskState

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
    from coscientist.core.claims.rubric import aggregate_rubric_status

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
    from coscientist.core.events.upcasters import upcast

    v, p = upcast(
        "TaskCreated",
        9,
        {
            "task_id": "t",
            "spec": {
                    "sandbox_check_limits": {
                    "exec_ok": {
                        "timeout_s": 5.0,
                        "memory_mb": 64,
                        "max_output_bytes": 1024,
                        "pids_max": 16,
                        "rlimit_nproc": 16,
                        "reap_timeout_s": 1.0,
                        "output_drain_join_s": 1.0,
                        "allow_network": False,
                        "allow_cgroup_degraded": False,
                    }
                },
                "task_key": "E1",
                "expected_outputs": [{"name": "main", "media_type": "text/plain"}],
            },
        },
    )
    # PR13 rubric (v10) + PR11 loop (v11) + PR15 kind (v12) + evidence_kind (v13).
    assert v == 14
    assert p["spec"]["expected_outputs"][0]["rubric"] is None
    assert p["spec"]["expected_outputs"][0]["kind"] is None
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
    assert v2 == 5  # PR15 kind v5 after PR16 decomposes v4
    assert p2["added_tasks"][0]["expected_outputs"][0]["rubric"] is None
    assert p2["added_tasks"][0]["loop_id"] is None
    assert p2["added_tasks"][0]["expected_outputs"][0]["kind"] is None
    assert p2["added_questions"][0]["final_type"] == "verdict"
    assert p2.get("rollup") == "all"
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
    assert v3 == 12  # G4 Need bumps PlanAccepted to v12
    assert p3["questions"][0]["final_type"] == "verdict"
    assert p3.get("loops") == []
    assert p3.get("rollup") == "all"


def test_study_view_uses_study_fulfilled_final_types():
    """view must not stay fulfilled when study_fulfilled would downgrade (artifact+refuted)."""
    from coscientist.core.execution.domain import Task, TaskKind, TaskState
    from coscientist.core.finalize.study_view import study_view

    rub = _judge_only_rubric()
    t = Task(
        task_id="s/E0",
        kind=TaskKind.EXPERIMENT,
        spec=_exp("E0", addresses=["Q1"], rubric=rub, criteria=[]),
        state=TaskState.DONE,
        deadline=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc),
        outcome=TaskOutcome.SUCCESS,
        attempts=["a0"],
    )
    # Task-level SUCCESS would be fulfilled, but artifact+refuted must cap at partial.
    view = study_view(
        [t],
        finalized=True,
        question_dispositions={"Q1": {"disposition": "refuted", "verdict": "refuted"}},
        question_final_types={"Q1": "artifact"},
    )
    assert view.verdict == "partial"
    view2 = study_view(
        [t],
        finalized=True,
        question_dispositions={"Q1": {"disposition": "untested"}},
        question_final_types={"Q1": "verdict"},
    )
    assert view2.verdict == "partial"


def test_study_fulfilled_wires_final_types():
    from coscientist.core.claims.rules import study_fulfilled

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
    # Decision: these block fulfilled for every final_type (incl. verdict).
    for ft in ("verdict", "artifact", "both"):
        for bad in ("not_attempted", "inconclusive", "rubric_not_met", "rubric_pending", "untested"):
            assert not study_fulfilled(
                {"Q1": bad},
                True,
                question_final_types={"Q1": ft},
            ), (ft, bad)


def test_review_key_scoped_no_cross_attempt():
    from coscientist.checks.kinds.rubric import evaluate_rubric

    rub = _judge_only_rubric()
    check = CodeCheck(kind="rubric", output="main", rubric=rub, allow_network=False, allow_cgroup_degraded=False)
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


def test_review_key_empty_fields_fail_closed():
    with pytest.raises(ValueError, match="non-empty"):
        review_key(
            task_id="",
            attempt_id="a",
            check_id="c",
            item_id="i",
            output_digest="d",
        )
    with pytest.raises(ValueError, match="non-empty"):
        evaluate_rubric(
            CodeCheck(
            kind="rubric",
            output="main",
            rubric=_judge_only_rubric(),
            allow_network=False,
            allow_cgroup_degraded=False,
        ),
            template_id="r",
            check_id="r#0",
            out=AgentTaskOutput(summary="x"),
            artifacts=None,
            role="validity",
            task_id="",
            attempt_id="a1",
            output_digest="d",
        )


def test_plan_refused_idem_hashes_full_detail():
    from coscientist.core.claims.reviews import plan_refused_idem
    from coscientist.core.events.digest import sha256_hex
    from coscientist.core.events.events import make_idem
    from coscientist.core.plan.plan_validate import refuse_detail

    long = "x" * 500
    full = refuse_detail(long)
    assert len(full) == 500
    assert plan_refused_idem("s1", full) == make_idem("PlanRefused", "s1", sha256_hex(full.encode("utf-8")))
    # Truncated snippet must not be what we hash.
    truncated = refuse_detail(long, truncate=True)
    assert len(truncated) == 200
    assert plan_refused_idem("s1", truncated) != plan_refused_idem("s1", full)


def test_both_rubric_pending_own_reason(clock):
    """both + verdict present + rubric pending → reason rubric_pending (not no_counting_claims)."""
    from unittest.mock import patch

    from coscientist.core.claims.claims import RuntimeClaim
    from coscientist.core.claims.dispositions import dispositions
    from coscientist.core.execution.domain import Task, TaskKind, TaskState

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
            "questions": [{"id": "Q1", "text": "q", "final_type": "both"}],
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
                    "status": "PENDING",
                    "effect": {"rubric_digest": rub.digest, "rubric_kind": "rubric"},
                }
            ],
        }
    }
    claim = RuntimeClaim(
        criterion_status="met",
        accepted=True,
        provenance="real",
        validity="valid",
        claim_outcome="consistent",
        check_role="prediction",
        frozen=True,
        evidence_kind="experimental",
        target="Q1",
        target_kind="question",
        validity_ok=True,
    )
    assert claim.counts()
    with (
        patch(
            "coscientist.core.claims.dispositions.filtered_claims_by_target",
            return_value={"Q1": [claim]},
        ),
        patch(
            "coscientist.core.claims.dispositions.claims_for_ref",
            return_value=[claim],
        ),
    ):
        disp = dispositions(
            tasks={"s/E0": task},
            plans=plans,
            graph=None,
            study=None,
            results=results,
        )
    row = disp["Q1"]
    assert row.get("reason") == "rubric_pending"
    assert row.get("disposition") == "not_attempted"
    assert row.get("reason") != NotAttemptedReason.NO_COUNTING_CLAIMS.value


@pytest.mark.asyncio
async def test_planner_validation_error_taskspec_dict_plan_refused(tmp_path, clock):
    """Pydantic ValidationError building TaskSpec/OutputSpec → PlanRefused."""
    rt = await _boot(tmp_path, clock, "st_pr13_pval")
    # Invalid: empty rubric items (Rubric validator) nested in OutputSpec via dict.
    bad = {
        "task_key": "E0",
        "title": "E0",
        "goal": "g",
        "route": "experiment",
        "depends_on": ["PLAN"],
        "addresses": ["Q1"],
        "criteria": [],
        "expected_outputs": [
            {
                "name": "main",
                "media_type": "text/markdown",
                "role": "report",
                "rubric": {"items": [], "aggregate": "all_required"},
            }
        ],
    }
    pid = await rt.accept_plan(
        [bad],  # type: ignore[list-item]
        questions=[QuestionSpec(id="Q1", text="q", final_type="artifact")],
        **TEST_NEED_KW,
    )
    assert pid == ""
    events = await rt.store.read_all()
    assert any(e.type == "PlanRefused" for e in events)
    assert any(e.type == "StudyFinalized" for e in events)


async def _assert_accept_plan_refused_finalizes(rt, *, pid: str, detail_substr: str | None = None) -> None:
    """Canonical PlanRefused path: empty plan_id + PlanRefused + StudyFinalized(REFUSED)."""
    assert pid == ""
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "PlanRefused"]
    assert len(refused) == 1
    if detail_substr is not None:
        assert detail_substr in str(refused[0].payload.get("detail") or "")
    assert refused[0].payload.get("report_line")
    fin = [e for e in events if e.type == "StudyFinalized"]
    assert len(fin) == 1
    payload = fin[0].payload
    assert payload.get("trigger") == "intake"
    assert payload.get("terminal_reason") == "refused"
    # PlanRefused finalize builds a real report artifact (not minimal fallback).
    assert payload.get("report_artifact_id") is not None
    assert payload.get("minimal_report") is None
    cids = payload.get("conclusion_ids")
    assert isinstance(cids, list) and len(cids) >= 1, cids
    assert rt.view().phase == "closed"
    assert rt.view().verdict == payload.get("verdict")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_cyclic_dag_plan_refused(tmp_path, clock):
    """DAG cycle in accept_plan → PlanRefused + StudyFinalized (no raw ValueError)."""
    rt = await _boot(tmp_path, clock, "st_pr13_dag")
    a = _exp("E0", addresses=["Q1"], rubric=_judge_only_rubric(), criteria=[], depends_on=["E1"])
    b = _exp("E1", addresses=["Q1"], rubric=_judge_only_rubric(), criteria=[], depends_on=["E0"])
    pid = await rt.accept_plan(
        [a, b],
        questions=[QuestionSpec(id="Q1", text="q", final_type="artifact")],
        **TEST_NEED_KW,
    )
    await _assert_accept_plan_refused_finalizes(rt, pid=pid, detail_substr="cyclic")


@pytest.mark.asyncio
async def test_accept_plan_unknown_expand_after_plan_refused(tmp_path, clock):
    """Unknown expand_after task_key → PlanRefused + StudyFinalized."""
    rt = await _boot(tmp_path, clock, "st_pr13_expand_after")
    pid = await rt.accept_plan(
        [_exp("E0", addresses=["Q1"], rubric=_judge_only_rubric(), criteria=[])],
        questions=[QuestionSpec(id="Q1", text="q", final_type="artifact")],
        expand_after=["MISSING"],
        **TEST_NEED_KW,
    )
    await _assert_accept_plan_refused_finalizes(rt, pid=pid, detail_substr="expand_after")


@pytest.mark.asyncio
async def test_accept_plan_shares_critic_plan_refused(tmp_path, clock):
    """Σ budget_share over reserve → PlanRefused + StudyFinalized (shares critic)."""
    rt = await _boot(tmp_path, clock, "st_pr13_shares")
    a = _exp("E0", addresses=["Q1"], rubric=_judge_only_rubric(), criteria=[])
    b = _exp("E1", addresses=["Q1"], rubric=_judge_only_rubric(), criteria=[], depends_on=["E0"])
    a = a.model_copy(update={"budget_share": 0.6})
    b = b.model_copy(update={"budget_share": 0.5})
    pid = await rt.accept_plan(
        [a, b],
        questions=[QuestionSpec(id="Q1", text="q", final_type="artifact")],
        **TEST_NEED_KW,
    )
    await _assert_accept_plan_refused_finalizes(rt, pid=pid, detail_substr="budget_share")


@pytest.mark.asyncio
async def test_accept_plan_unknown_dependency_plan_refused(tmp_path, clock):
    """Unknown depends_on key → PlanRefused + StudyFinalized."""
    rt = await _boot(tmp_path, clock, "st_pr13_unk_dep")
    bad = _exp(
        "E0",
        addresses=["Q1"],
        rubric=_judge_only_rubric(),
        criteria=[],
        depends_on=["NO_SUCH_TASK"],
    )
    pid = await rt.accept_plan(
        [bad],
        questions=[QuestionSpec(id="Q1", text="q", final_type="artifact")],
        **TEST_NEED_KW,
    )
    await _assert_accept_plan_refused_finalizes(rt, pid=pid, detail_substr="unknown dependency")


@pytest.mark.asyncio
async def test_e2e_fulfilled_view_matches_payload(tmp_path, clock):
    """Production path: stages succeed → artifact answered → fulfilled; view == payload."""
    rt, _ex, arts = await _boot_stages_ok(tmp_path, clock, "st_pr13_fulfilled")
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
        **TEST_NEED_KW,
    )
    ex, arts = attach_executor(rt, tmp_path)
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
    assert task.outcome == TaskOutcome.SUCCESS
    await _complete_report_and_finalize(rt, tmp_path)
    fin = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fin) == 1
    payload = fin[-1].payload
    assert payload.get("verdict") == "fulfilled"
    assert rt.view().phase == "closed"
    assert rt.view().verdict == payload.get("verdict") == "fulfilled"
    qdisp = payload.get("question_dispositions") or {}
    assert qdisp.get("Q1", {}).get("disposition") == "answered"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_e2e_downgrade_unaddressed_question_view_matches(tmp_path, clock):
    """All required SUCCESS but unaddressed question → partial; view == payload."""
    rt, _ex, arts = await _boot_stages_ok(tmp_path, clock, "st_pr13_downgrade")
    # E0 does not address Q1 → disposition not_attempted; tasks can still all SUCCESS.
    await rt.accept_plan(
        [_exp("E0", addresses=[], rubric=None, criteria=[RequirementRef(template_id="artifact_present")])],
        questions=[QuestionSpec(id="Q1", text="orphan question", final_type="verdict")],
        **TEST_NEED_KW,
    )
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("E0")
    assert aid is not None
    art = arts.put(
        b"{}",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="application/json",
    )
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 1},
        ),
    )
    await _complete_report_and_finalize(rt, tmp_path)
    fin = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fin) == 1
    payload = fin[-1].payload
    assert payload.get("verdict") == "partial"
    assert rt.view().verdict == payload.get("verdict") == "partial"
    qdisp = payload.get("question_dispositions") or {}
    assert qdisp.get("Q1", {}).get("disposition") == "not_attempted"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_corrupt_retry_value_finalizes_internal_error(tmp_path, clock):
    """Corrupt ReviewRecorded.retry → JudgePendingCorrupt → finalize(internal_error)."""
    from coscientist.core.claims.reviews import JudgePendingCorrupt, rebuild_judge_state

    # Unit: rebuild must raise on non-int retry (fail-closed).
    bad = EventEnvelope(
        study_id="s",
        type="ReviewRecorded",
        entity_key="a1",
        idem_key="x",
        payload={
            "review_key": '["t","a1","c","i","d"]',
            "status": "PENDING",
            "retry": "not-an-int",
            "attempt_id": "a1",
        },
        ts=clock.now(),
    )
    with pytest.raises(JudgePendingCorrupt, match="retry_corrupt"):
        rebuild_judge_state([bad], artifacts=None)

    # E2E: journal with corrupt retry on reopen → honest internal-error finalize.
    sid = "st_pr13_retry_corrupt"
    rt = await _boot(tmp_path, clock, sid)
    rub = freeze_rubric(
        Rubric(
            items=[RubricItem(id="adequate", text="ok?", required=True, check="judge")],
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
        **TEST_NEED_KW,
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
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="draft",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        ),
    )
    # Append a corrupt ReviewRecorded into the journal before reopen.
    dig = ex._output_digests[aid]
    key = review_key(
        task_id=str(rt.attempts[aid]["task_id"]),
        attempt_id=aid,
        check_id=f"rubric:{rub.digest}#out",
        item_id="adequate",
        output_digest=dig,
    )
    await rt._append(
        [
            EventEnvelope(
                study_id=sid,
                type="ReviewRecorded",
                entity_key=aid,
                idem_key=make_idem("ReviewRecorded", aid, key, "corrupt_retry"),
                payload={
                    "review_key": key,
                    "attempt_id": aid,
                    "task_id": rt.attempts[aid]["task_id"],
                    "check_id": f"rubric:{rub.digest}#out",
                    "item_id": "adequate",
                    "output_digest": dig,
                    "status": "PENDING",
                    "failure_kind": "timeout",
                    "retry": {"bad": True},
                },
                ts=rt.clock.now(),
            )
        ]
    )
    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    events = await rt2.store.read_all()
    assert any(e.type == "JudgeCasCorrupt" for e in events)
    fin = [e for e in events if e.type == "StudyFinalized"]
    assert len(fin) == 1
    assert fin[0].payload.get("trigger") == "internal_error"
    assert fin[0].payload.get("terminal_reason") == "internal_error"
    assert rt2.view().phase == "closed"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_corrupt_judge_cas_finalizes_internal_error(tmp_path, clock):
    """Corrupt pending CAS on reopen → JudgeCasCorrupt + StudyFinalized (no hang)."""
    sid = "st_pr13_cas_corrupt"
    rt = await _boot(tmp_path, clock, sid)
    rub = freeze_rubric(
        Rubric(
            items=[RubricItem(id="adequate", text="ok?", required=True, check="judge")],
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
        **TEST_NEED_KW,
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
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="draft",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        ),
    )
    dig = ex._output_digests[aid]
    await close_runtime(rt)
    # Corrupt the CAS blob for the pending digest (study-local arts/cas/<digest>).
    cas_path = tmp_path / sid / "arts" / "cas" / dig
    assert cas_path.exists(), cas_path
    cas_path.write_bytes(b"NOT-JSON{{{")

    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    events = await rt2.store.read_all()
    assert any(e.type == "JudgeCasCorrupt" for e in events)
    fin = [e for e in events if e.type == "StudyFinalized"]
    assert len(fin) == 1
    assert fin[0].payload.get("trigger") == "internal_error"
    assert fin[0].payload.get("terminal_reason") == "internal_error"
    contrib = fin[0].payload.get("contributing_reasons") or []
    assert any("judge CAS" in str(x) for x in contrib)
    assert rt2.view().phase == "closed"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_rubric_failed_does_not_invalidate_claims_both(tmp_path, clock):
    """final_type=both via executor: failed rubric ≠ claim validity_failed (PR 8b)."""
    rt = await _boot(tmp_path, clock, "st_pr13_validity_ok")
    rub = _judge_only_rubric()
    await rt.accept_plan(
        [
            _exp(
                "E0",
                addresses=["Q1"],
                rubric=rub,
                criteria=[RequirementRef(template_id="artifact_present")],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="both?", final_type="both")],
        **TEST_NEED_KW,
    )
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("E0")
    assert aid is not None
    art = arts.put(
        b"body",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="text/markdown",
    )
    out_check = f"rubric:{rub.digest}#out"
    pending = await ex.record(
        aid,
        AgentTaskOutput(
            summary="claim",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            claims=[
                {
                    "text": "c1",
                    "polarity": "supports",
                    "target": "Q1",
                    "about": "Q1",
                }
            ],
        ),
    )
    assert pending.reason == "required_checks_pending"
    # Judge not-met → rubric FAILED; compute_outcome is PARTIAL (artifact MET + rubric FAILED).
    final = await ex.record_review(aid, out_check, item_id="adequate", status=ReqStatus.FAILED)
    assert final.outcome.value == "PARTIAL"
    recorded = next(e for e in await rt.store.read_all() if e.type == "ResultRecorded")
    rub_cr = next(
        c
        for c in (recorded.payload.get("check_results") or [])
        if str(c.get("check_id") or "").startswith("rubric:")
    )
    assert rub_cr.get("status") == "FAILED"
    assert rub_cr.get("role") == "validity"
    # Claims land as GraphNodeUpserted — failed rubric must not mark validity_failed.
    claim_nodes = [
        e
        for e in await rt.store.read_all()
        if e.type == "GraphNodeUpserted" and ":claim:" in str(e.payload.get("node_id") or "")
    ]
    assert claim_nodes, "executor must upsert claim nodes"
    for e in claim_nodes:
        attrs = e.payload.get("attrs") or {}
        assert attrs.get("validity_ok") is True
        assert attrs.get("exclusion_reason") != "validity_failed"
    await close_runtime(rt)


def test_met_one_task_never_covers_another(clock):
    """Documented invariant: MET on E0 does not satisfy E1's rubric."""
    from coscientist.core.claims.rubric import task_rubric_status
    from coscientist.core.execution.domain import Task, TaskKind, TaskState

    rub = _judge_only_rubric()
    t0 = Task(
        task_id="s/E0",
        kind=TaskKind.EXPERIMENT,
        spec=_exp("E0", addresses=["Q1"], rubric=rub, criteria=[]),
        state=TaskState.DONE,
        deadline=clock.now() + timedelta(hours=1),
        outcome=TaskOutcome.SUCCESS,
        attempts=["a0"],
    )
    t1 = Task(
        task_id="s/E1",
        kind=TaskKind.EXPERIMENT,
        spec=_exp("E1", addresses=["Q1"], rubric=rub, criteria=[]),
        state=TaskState.DONE,
        deadline=clock.now() + timedelta(hours=1),
        outcome=TaskOutcome.SUCCESS,
        attempts=["a1"],
    )
    results = {
        "a0": {
            "attempt_id": "a0",
            "check_results": [
                {
                    "check_id": f"rubric:{rub.digest}#out",
                    "template_id": f"rubric:{rub.digest}",
                    "status": "MET",
                    "effect": {"rubric_digest": rub.digest, "rubric_kind": "rubric"},
                }
            ],
        },
        "a1": {
            "attempt_id": "a1",
            "check_results": [
                {
                    "check_id": f"rubric:{rub.digest}#out",
                    "template_id": f"rubric:{rub.digest}",
                    "status": "FAILED",
                    "effect": {"rubric_digest": rub.digest, "rubric_kind": "rubric"},
                }
            ],
        },
    }
    assert task_rubric_status(task_keys={"E0"}, tasks={"s/E0": t0, "s/E1": t1}, results=results) == "met"
    assert task_rubric_status(task_keys={"E1"}, tasks={"s/E0": t0, "s/E1": t1}, results=results) == "failed"
    # Covering both: met wins if any covering task met (independent evaluation).
    assert task_rubric_status(task_keys={"E0", "E1"}, tasks={"s/E0": t0, "s/E1": t1}, results=results) == "met"
