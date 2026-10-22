"""State transitions §3.3 / crash recovery §3.7."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.clock import FakeClock
from coscientist.core.domain import TaskOutcome, TaskSpec, TaskState
from coscientist.core.recover import recover
from coscientist.core.runtime_factory import close_runtime, open_runtime


async def _boot(tmp_path: Path, clock: FakeClock, seconds: int = 60):
    rt = await open_runtime(tmp_path, "st_tr", owner="o", clock=clock)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=seconds),
        sid="st_tr",
    )
    return rt


@pytest.mark.asyncio
async def test_p1_p2_p4_happy_path(tmp_path, clock):
    rt = await _boot(tmp_path, clock)
    aid = await rt.start_attempt("SPEC")
    assert rt.tasks["st_tr/SPEC"].state == TaskState.RUNNING
    await rt.finish_attempt(aid)
    assert rt.tasks["st_tr/SPEC"].state == TaskState.DONE
    assert rt.tasks["st_tr/SPEC"].outcome == TaskOutcome.SUCCESS
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_p3_retry_then_done(tmp_path, clock):
    rt = await _boot(tmp_path, clock, seconds=120)
    aid = await rt.start_attempt("SPEC")
    await rt.fail_attempt(aid, retryable=True)
    assert rt.tasks["st_tr/SPEC"].state == TaskState.RUNNING
    aid2 = await rt.start_attempt("SPEC")
    await rt.finish_attempt(aid2)
    assert rt.tasks["st_tr/SPEC"].outcome == TaskOutcome.SUCCESS
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_p5_deadline_fails_task(tmp_path, clock):
    rt = await _boot(tmp_path, clock, seconds=10)
    await rt.start_attempt("SPEC", hang=True)
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
    # either DONE or RUNNING with new attempt
    t = rt.tasks["st_tr/SPEC"]
    assert t.state in (TaskState.DONE, TaskState.RUNNING)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_llm_hang_past_deadline(tmp_path, clock):
    """Fake hanging coroutine (no real LLM); deadline cancels and rejects late finish."""
    rt = await _boot(tmp_path, clock, seconds=5)
    aid = await rt.start_attempt("SPEC", hang=True)
    clock.advance(timedelta(seconds=6))
    await rt.tick()
    t = rt.tasks["st_tr/SPEC"]
    assert t.state == TaskState.DONE
    assert t.outcome == TaskOutcome.FAILED
    assert rt.attempts[aid]["closed"] is True
    # late finish rejected
    with pytest.raises(RuntimeError, match="closed"):
        await rt.finish_attempt(aid)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_creates_experiments(tmp_path, clock):
    rt = await _boot(tmp_path, clock, seconds=300)
    for key in ("SPEC", "HYP", "LIT"):
        aid = await rt.start_attempt(key)
        await rt.finish_attempt(aid)
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="EXP-1",
                title="e1",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
            ),
            TaskSpec(
                task_key="EXP-2",
                title="e2",
                goal="g",
                route="experiment",
                depends_on=["EXP-1"],
            ),
        ]
    )
    assert "st_tr/EXP-1" in rt.tasks
    assert rt.tasks["st_tr/PLAN"].state == TaskState.DONE
    await close_runtime(rt)
