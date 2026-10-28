"""Human commands pass through the live owner and produce durable decisions."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.agents.schemas import PlanOut
from coscientist.api.hitl import AnswerBody, HitlOwner, HitlRefuse
from coscientist.api.hitl_routes import hitl_rows
from coscientist.cli.errors import CliRefuse
from coscientist.cli.run import _stage_gate_review
from coscientist.core.events.errors import LeaseLost
from coscientist.core.events.schema_gate import upcast_strict
from coscientist.core.execution.domain import Need
from tests.experimental.test_hitl import _boot
from tests.helpers import attach_executor, close_runtime, record_success


def _plan() -> PlanOut:
    return PlanOut(tasks=(), questions=(), hypotheses=Need(kind="need", needed=False, reason="test"),
                   literature=Need(kind="need", needed=False, reason="test"))


async def _waiting(owner: HitlOwner, task: asyncio.Task[bool]) -> None:
    async with asyncio.timeout(5):
        while owner.waiting is None:
            if task.done():
                await task
                raise AssertionError("owner finished without opening a gate")
            await asyncio.sleep(0)


async def _setup(tmp_path, clock):
    rt = await _boot(tmp_path, clock, seconds=120)
    _, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await record_success(rt, aid, arts)
    assert await rt.start_attempt("PLAN") is not None
    owner = HitlOwner(poll_s=0.001, lease_ttl_s=600)
    task = asyncio.create_task(owner.review_plan(rt, _plan()))
    await _waiting(owner, task)
    rid = owner.waiting[1]
    opened = next(e for e in await rt.store.read_all() if e.type == "HitlOpened")
    return rt, owner, task, rid, opened


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["accept", "reject"])
async def test_owner_answer_ack_is_journaled(tmp_path, clock, decision):
    rt, owner, task, rid, opened = await _setup(tmp_path, clock)
    try:
        prompt = opened.payload["prompt"]
        assert hashlib.sha256(prompt["context_json"].encode()).hexdigest() == prompt["context_sha256"]
        ack = await owner.submit(rt.study.study_id, rid, AnswerBody(
            decision=decision, opened_seq=opened.seq, context_sha256=prompt["context_sha256"],
        ))
        assert await task is (decision == "accept")
        events = await rt.store.read_all()
        event = next(e for e in events if e.seq == ack["seq"])
        assert event.type == "HitlDecided" and event.payload["decision"] == decision
        assert not rt.hitl[rid].open
        assert not any(e.type == "PlanAccepted" for e in events)
        assert any(e.type == "StudyFinalized" for e in events) is (decision == "reject")
        with pytest.raises(HitlRefuse, match="not_waiting"):
            await owner.submit(rt.study.study_id, rid, AnswerBody(
                decision=decision, opened_seq=opened.seq, context_sha256=prompt["context_sha256"],
            ))
    finally:
        owner.close()
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_stale_snapshot_refused_then_current_answer_works(tmp_path, clock):
    rt, owner, task, rid, opened = await _setup(tmp_path, clock)
    try:
        for seq, digest in [(opened.seq + 1, opened.payload["prompt"]["context_sha256"]), (opened.seq, "0" * 64)]:
            with pytest.raises(HitlRefuse, match="stale_context"):
                await owner.submit(rt.study.study_id, rid, AnswerBody(
                    decision="accept", opened_seq=seq, context_sha256=digest,
                ))
        assert rt.hitl[rid].open
        await owner.submit(rt.study.study_id, rid, AnswerBody(
            decision="accept", opened_seq=opened.seq, context_sha256=opened.payload["prompt"]["context_sha256"],
        ))
        assert await task
    finally:
        owner.close()
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_timeout_never_delegates_approval(tmp_path, clock):
    rt, owner, task, rid, opened = await _setup(tmp_path, clock)
    try:
        clock.advance(timedelta(seconds=121))
        assert not await asyncio.wait_for(task, timeout=5)
        events = await rt.store.read_all()
        assert any(e.type == "HitlTimedOut" for e in events)
        assert not any(e.type == "HitlDecided" for e in events)
        assert not any(e.type == "PlanAccepted" for e in events)
        rows = hitl_rows([{"type": e.type, "payload": e.payload, "seq": e.seq} for e in events])
        assert rows[0]["status"] == "timed_out"
    finally:
        owner.close()
        await close_runtime(rt)


def test_old_hitl_upcast_records_absent_prompt():
    payload = dict(request_id="h", kind="plan", open=True, task_id="t", cost_rub=0.0)
    version, replay = upcast_strict("HitlOpened", 1, payload)
    assert version == 2 and replay["prompt"] is None
    assert "prompt" not in payload


@pytest.mark.asyncio
async def test_cancellation_resolves_inflight_command_without_ack(tmp_path, clock, monkeypatch):
    rt, owner, task, rid, opened = await _setup(tmp_path, clock)
    entered = asyncio.Event()

    async def blocked_answer(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(rt, "answer_hitl", blocked_answer)
    body = AnswerBody(decision="accept", opened_seq=opened.seq,
                      context_sha256=opened.payload["prompt"]["context_sha256"])
    submit = asyncio.create_task(owner.submit(rt.study.study_id, rid, body))
    await asyncio.wait_for(entered.wait(), timeout=5)
    with pytest.raises(HitlRefuse, match="in_progress"):
        await owner.submit(rt.study.study_id, rid, body)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(HitlRefuse, match="owner_closed"):
        await submit
    assert not any(e.type == "HitlDecided" for e in await rt.store.read_all())
    owner.close()
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalized_interactive_tick_never_times_out_old_gate(tmp_path, clock):
    rt = await _boot(tmp_path, clock, seconds=120)
    try:
        await rt.ensure_lease(ttl_s=600)
        await rt.open_hitl("PLAN", kind="plan_approval", timeout_s=1)
        await rt.cancel_study(by="operator")
        assert rt._finalized
        events = await rt.store.read_all()
        clock.advance(timedelta(seconds=2))
        await rt.tick()
        assert await rt.store.read_all() == events
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalized_interactive_tick_still_fences_lease(tmp_path, clock):
    rt = await _boot(tmp_path, clock, seconds=120)
    try:
        await rt.cancel_study(by="operator")
        events = await rt.store.read_all()
        await rt.store.release_lease(rt.owner, rt.store.epoch)
        with pytest.raises(LeaseLost):
            await rt.tick()
        assert await rt.store.read_all() == events
    finally:
        await close_runtime(rt)


def test_answer_body_feedback_contract():
    """HITL v2: feedback is optional 0..4000 text, mandatory non-empty for revise."""
    with pytest.raises(ValidationError):
        AnswerBody(decision="revise", opened_seq=1, context_sha256="a" * 64)
    with pytest.raises(ValidationError):
        AnswerBody(decision="reject", opened_seq=1, context_sha256="a" * 64, feedback="x" * 4001)
    body = AnswerBody(decision="reject", opened_seq=1, context_sha256="a" * 64, feedback="  текст  ")
    assert body.feedback == "текст"
    blank = AnswerBody(decision="accept", opened_seq=1, context_sha256="a" * 64, feedback="   ")
    assert blank.feedback is None
    plain = AnswerBody(decision="accept", opened_seq=1, context_sha256="a" * 64)
    assert plain.feedback is None


async def _opening(rt: Any, rid: str) -> Any:
    events = await rt.store.read_all()
    return next(e for e in reversed(events) if e.type == "HitlOpened" and e.payload["request_id"] == rid)


@pytest.mark.asyncio
async def test_owner_feedback_rides_hitl_decided_and_plan_revise_refused(tmp_path, clock):
    """accept+feedback stores the verbatim text; plan gates refuse revise (§2 contract)."""
    rt, owner, task, rid, opened = await _setup(tmp_path, clock)
    try:
        with pytest.raises(HitlRefuse, match="hitl_revise_needs_stage_gate"):
            await owner.submit(rt.study.study_id, rid, AnswerBody(
                decision="revise", opened_seq=opened.seq,
                context_sha256=opened.payload["prompt"]["context_sha256"],
                feedback="поменяй план",
            ))
        assert rt.hitl[rid].open
        ack = await owner.submit(rt.study.study_id, rid, AnswerBody(
            decision="accept", opened_seq=opened.seq,
            context_sha256=opened.payload["prompt"]["context_sha256"],
            feedback="  принято с замечанием  ",
        ))
        assert await task is True
        decided = next(
            e for e in await rt.store.read_all()
            if e.type == "HitlDecided" and e.payload["request_id"] == rid
        )
        assert decided.payload["feedback"] == "принято с замечанием"
        assert ack["decision"] == "accept"
    finally:
        owner.close()
        await close_runtime(rt)


async def _stage_gate_setup(tmp_path, clock):
    rt = await _boot(tmp_path, clock, seconds=600)
    attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    owner = HitlOwner(poll_s=0.001, lease_ttl_s=600)
    return rt, owner, aid


async def _await_second_gate(owner: HitlOwner, driver: asyncio.Task[Any], first_rid: str) -> str:
    async with asyncio.timeout(5):
        while True:
            if driver.done():
                await driver
                raise AssertionError("driver finished without the second gate")
            if owner.waiting is not None and owner.waiting[1] != first_rid:
                return owner.waiting[1]
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stage_gate_revise_repair_round_accepts_corrected_output(tmp_path, clock):
    """revise + feedback → the exact text reaches the repair callback; corrected
    output opens a fresh gate; accept closes the driver with the repaired output."""
    rt, owner, aid = await _stage_gate_setup(tmp_path, clock)
    usages: list[dict[str, object]] = []
    repairs: list[str] = []

    async def repair(feedback: str) -> dict[str, object]:
        repairs.append(feedback)
        return {"version": 2}

    driver = asyncio.create_task(_stage_gate_review(
        rt, owner,
        kind="spec_approval", task_key="SPEC", attempt_id=aid,
        question="Принять это ТЗ (спецификацию исследования)?",
        output={"version": 1},
        snapshot=lambda out: dict(out),
        run_repair=repair,
        usages=usages,
        gate_label="spec_approval gate",
    ))
    try:
        await _waiting(owner, driver)
        rid1 = owner.waiting[1]
        opening = await _opening(rt, rid1)
        await owner.submit(rt.study.study_id, rid1, AnswerBody(
            decision="revise", opened_seq=opening.seq,
            context_sha256=opening.payload["prompt"]["context_sha256"],
            feedback="  сузь объём работ  ",
        ))
        async with asyncio.timeout(5):
            while not repairs:
                if driver.done():
                    await driver
                    raise AssertionError("driver finished without a repair round")
                await asyncio.sleep(0)
        assert repairs == ["сузь объём работ"]
        rid2 = await _await_second_gate(owner, driver, rid1)
        second = await _opening(rt, rid2)
        assert second.payload["prompt"]["context_json"] == '{"version":2}'
        await owner.submit(rt.study.study_id, rid2, AnswerBody(
            decision="accept", opened_seq=second.seq,
            context_sha256=second.payload["prompt"]["context_sha256"],
        ))
        output, record = await asyncio.wait_for(driver, timeout=5)
        assert output == {"version": 2}
        assert record["decision"] == "accept"
        decided = [e for e in await rt.store.read_all() if e.type == "HitlDecided"]
        assert decided[0].payload["decision"] == "revise"
        assert decided[0].payload["feedback"] == "сузь объём работ"
        assert not any(e.type == "StudyCancelled" for e in await rt.store.read_all())
    finally:
        owner.close()
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_stage_gate_second_revise_refuses_typed_with_feedback(tmp_path, clock):
    """A second return after the one bounded repair round cancels the study and
    refuses typed, naming the gate and the human feedback (owner law)."""
    rt, owner, aid = await _stage_gate_setup(tmp_path, clock)
    usages: list[dict[str, object]] = []

    async def repair(feedback: str) -> dict[str, object]:
        return {"version": 2}

    driver = asyncio.create_task(_stage_gate_review(
        rt, owner,
        kind="hypotheses", task_key="SPEC", attempt_id=aid,
        question="Принять гипотезы?",
        output={"version": 1},
        snapshot=lambda out: dict(out),
        run_repair=repair,
        usages=usages,
        gate_label="hypotheses gate",
    ))
    try:
        await _waiting(owner, driver)
        rid1 = owner.waiting[1]
        opening = await _opening(rt, rid1)
        await owner.submit(rt.study.study_id, rid1, AnswerBody(
            decision="reject", opened_seq=opening.seq,
            context_sha256=opening.payload["prompt"]["context_sha256"],
            feedback="опять не то",
        ))
        rid2 = await _await_second_gate(owner, driver, rid1)
        second = await _opening(rt, rid2)
        await owner.submit(rt.study.study_id, rid2, AnswerBody(
            decision="revise", opened_seq=second.seq,
            context_sha256=second.payload["prompt"]["context_sha256"],
            feedback="всё ещё не то",
        ))
        with pytest.raises(CliRefuse, match="hypotheses gate: human repair rounds exhausted; feedback: всё ещё не то"):
            await asyncio.wait_for(driver, timeout=5)
        events = await rt.store.read_all()
        cancels = [e for e in events if e.type == "StudyCancelled"]
        assert cancels and cancels[-1].payload["by"] == "human-hypotheses-rejected"
    finally:
        owner.close()
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_stage_gate_bare_reject_without_feedback_cancels(tmp_path, clock):
    rt, owner, aid = await _stage_gate_setup(tmp_path, clock)
    usages: list[dict[str, object]] = []

    async def repair(feedback: str) -> dict[str, object]:
        raise AssertionError("repair must not run without feedback")

    driver = asyncio.create_task(_stage_gate_review(
        rt, owner,
        kind="spec_approval", task_key="SPEC", attempt_id=aid,
        question="Принять это ТЗ?",
        output={"version": 1},
        snapshot=lambda out: dict(out),
        run_repair=repair,
        usages=usages,
        gate_label="spec_approval gate",
    ))
    try:
        await _waiting(owner, driver)
        rid = owner.waiting[1]
        opening = await _opening(rt, rid)
        await owner.submit(rt.study.study_id, rid, AnswerBody(
            decision="reject", opened_seq=opening.seq,
            context_sha256=opening.payload["prompt"]["context_sha256"],
        ))
        with pytest.raises(CliRefuse, match="spec_approval gate: human rejected without repair feedback"):
            await asyncio.wait_for(driver, timeout=5)
        events = await rt.store.read_all()
        cancels = [e for e in events if e.type == "StudyCancelled"]
        assert cancels and cancels[-1].payload["by"] == "human-spec_approval-rejected"
    finally:
        owner.close()
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_stage_gate_timeout_delegates_to_critic(tmp_path, clock):
    """§2/§5 semantics: silence on a stage gate delegates to the critic (never a
    study cancel like the plan gate); the decision rides the journal."""
    rt = await _boot(tmp_path, clock, seconds=120)
    attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    owner = HitlOwner(poll_s=0.001, lease_ttl_s=600)
    outcome_task = asyncio.create_task(owner.review_stage(
        rt, kind="spec_approval", task_key="SPEC", attempt_id=aid,
        question="Принять это ТЗ?", snapshot={"version": 1},
    ))
    try:
        await _waiting(owner, outcome_task)
        clock.advance(timedelta(seconds=130))
        outcome = await asyncio.wait_for(outcome_task, timeout=5)
        assert outcome.decision == "delegated"
        assert outcome.feedback is None
        events = await rt.store.read_all()
        assert any(e.type == "HitlTimedOut" for e in events)
        decided = next(e for e in events if e.type == "HitlDecided")
        assert decided.payload["by"] == "policy"
        assert decided.payload["approved_by"] == "critic"
        assert decided.payload["delegated_by"] == "timeout"
        assert decided.payload["decision"] == "accept"
        assert not any(e.type == "StudyCancelled" for e in events)
    finally:
        owner.close()
        await close_runtime(rt)
