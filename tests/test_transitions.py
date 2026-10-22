"""State transitions §3.3 / crash recovery §3.7."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.clock import FakeClock
from coscientist.core.domain import HitlPolicy, TaskOutcome, TaskState
from coscientist.core.recover import recover
from coscientist.core.runtime_factory import close_runtime, open_runtime
from tests.helpers import attach_executor, exp_spec, record_success


async def _boot(tmp_path: Path, clock: FakeClock, seconds: int = 60, **kw):
    rt = await open_runtime(tmp_path, "st_tr", clock=clock)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=seconds),
        sid="st_tr",
        **kw,
    )
    attach_executor(rt, tmp_path)
    return rt


@pytest.mark.asyncio
async def test_p1_p2_p4_happy_path(tmp_path, clock):
    rt = await _boot(tmp_path, clock)
    _, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    assert rt.tasks["st_tr/SPEC"].state == TaskState.RUNNING
    await record_success(rt, aid, arts)
    assert rt.tasks["st_tr/SPEC"].state == TaskState.DONE
    assert rt.tasks["st_tr/SPEC"].outcome == TaskOutcome.SUCCESS
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_p3_retry_then_done(tmp_path, clock):
    rt = await _boot(tmp_path, clock, seconds=120)
    _, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await rt.fail_attempt(aid, retryable=True)
    # P3: PENDING with retry_at — never RUNNING without an open attempt
    assert rt.tasks["st_tr/SPEC"].state == TaskState.PENDING
    assert rt.tasks["st_tr/SPEC"].reason == "retry_scheduled"
    assert "st_tr/SPEC" in rt._retry_at
    open_during_backoff = [
        a
        for a in rt.tasks["st_tr/SPEC"].attempts
        if not rt.attempts.get(a, {}).get("closed")
    ]
    assert open_during_backoff == []
    import asyncio

    await asyncio.sleep(0.1)
    open_aids = [
        a
        for a in rt.tasks["st_tr/SPEC"].attempts
        if not rt.attempts.get(a, {}).get("closed")
    ]
    assert len(open_aids) == 1
    assert rt.tasks["st_tr/SPEC"].state == TaskState.RUNNING
    await record_success(rt, open_aids[0], arts)
    assert rt.tasks["st_tr/SPEC"].outcome == TaskOutcome.SUCCESS
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_p5_deadline_fails_task(tmp_path, clock):
    rt = await _boot(tmp_path, clock, seconds=10)
    await rt.start_attempt("SPEC")
    clock.advance(timedelta(seconds=11))
    await rt.tick()
    t = rt.tasks["st_tr/SPEC"]
    assert t.state == TaskState.DONE
    assert t.outcome == TaskOutcome.FAILED
    assert t.reason == "deadline"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_p6_cancel_skipped(tmp_path, clock):
    rt = await _boot(tmp_path, clock)
    await rt.cancel_task("SPEC", reason="cancelled")
    t = rt.tasks["st_tr/SPEC"]
    assert t.state == TaskState.DONE
    assert t.outcome == TaskOutcome.SKIPPED
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_deps_must_be_done(tmp_path, clock):
    rt = await _boot(tmp_path, clock)
    with pytest.raises(RuntimeError, match="dependencies"):
        await rt.start_attempt("HYP")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_recover_closes_open_attempt(tmp_path, clock):
    rt = await _boot(tmp_path, clock, seconds=120)
    await rt.start_attempt("SPEC")
    result = await recover(rt)
    assert result["closed"] >= 1
    t = rt.tasks["st_tr/SPEC"]
    # P3 retryable close → PENDING(+retry_at) until backoff starts the next attempt
    assert t.state in (TaskState.DONE, TaskState.RUNNING, TaskState.PENDING)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_llm_hang_past_deadline(tmp_path, clock):
    """Test double: cancel attempt work via deadline (no hang flag in prod API)."""
    rt = await _boot(tmp_path, clock, seconds=5)
    aid = await rt.start_attempt("SPEC")

    async def _work():
        import asyncio

        try:
            await asyncio.sleep(10_000)
        except asyncio.CancelledError:
            return

    rt._attempt_tasks[aid] = __import__("asyncio").create_task(_work())
    clock.advance(timedelta(seconds=6))
    await rt.tick()
    t = rt.tasks["st_tr/SPEC"]
    assert t.state == TaskState.DONE
    assert t.outcome == TaskOutcome.FAILED
    assert rt.attempts[aid]["closed"] is True
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_creates_experiments(tmp_path, clock):
    rt = await _boot(tmp_path, clock, seconds=300)
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        aid = await rt.start_attempt(key)
        await record_success(rt, aid, arts)
    await rt.accept_plan([exp_spec("EXP-1"), exp_spec("EXP-2", ["EXP-1"])])
    assert "st_tr/EXP-1" in rt.tasks
    assert rt.tasks["st_tr/PLAN"].state == TaskState.DONE
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_hitl_delegate_records_critic(tmp_path, clock):
    rt = await _boot(
        tmp_path,
        clock,
        seconds=60,
        hitl_policy=HitlPolicy(on_timeout="delegate"),
    )
    _, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await record_success(rt, aid, arts)
    rid = await rt.open_hitl("PLAN", kind="plan_approval", timeout_s=1)
    clock.advance(timedelta(seconds=2))
    await rt.tick()
    assert rt.hitl[rid].open is False
    decided = [e for e in rt.store.read_all() if e.type == "HitlDecided"]
    assert any(e.payload.get("approved_by") == "critic" for e in decided)
    assert any(e.payload.get("delegated_by") == "timeout" for e in decided)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_hitl_human_delegate_records_critic(tmp_path, clock):
    """Non-costly answer_hitl('delegate') → critic + delegated_by=human (§7.3)."""
    rt = await _boot(tmp_path, clock, seconds=120)
    _, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await record_success(rt, aid, arts)
    rid = await rt.open_hitl("PLAN", kind="plan_approval", timeout_s=60)
    await rt.answer_hitl(rid, "delegate")
    assert rt.hitl[rid].open is False
    decided = [
        e for e in rt.store.read_all() if e.type == "HitlDecided" and e.entity_key == rid
    ]
    assert len(decided) == 1
    assert decided[0].payload.get("approved_by") == "critic"
    assert decided[0].payload.get("delegated_by") == "human"
    await close_runtime(rt)
