"""Human commands pass through the live owner and produce durable decisions."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

import pytest

from coscientist.agents.schemas import PlanOut
from coscientist.api.hitl import AnswerBody, HitlOwner, HitlRefuse
from coscientist.api.hitl_routes import hitl_rows
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

    async def blocked_answer(*args):
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
