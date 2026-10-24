"""State transitions §3.3 / crash recovery §3.7."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.errors import AttemptClosedError
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    RequirementRef,
    TaskOutcome,
    TaskState,
)
from coscientist.core.execution.recover import recover
from tests.helpers import attach_executor, close_runtime, exp_spec, open_runtime, record_success


async def _boot(tmp_path: Path, clock: FakeClock, runtime_cls: type, seconds: int = 60, **kw):
    rt = await open_runtime(tmp_path, "st_tr", clock=clock, runtime_cls=runtime_cls)
    kw.setdefault("finalize_reserve_s", 5 if seconds > 5 else 1)
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
async def test_p1_p2_p4_happy_path(tmp_path, clock, runtime_cls):
    rt = await _boot(tmp_path, clock, runtime_cls)
    _, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    assert rt.tasks["st_tr/SPEC"].state == TaskState.RUNNING
    await record_success(rt, aid, arts)
    assert rt.tasks["st_tr/SPEC"].state == TaskState.DONE
    assert rt.tasks["st_tr/SPEC"].outcome == TaskOutcome.SUCCESS
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_p3_retry_then_done(tmp_path, clock, runtime_cls):
    from coscientist.core.execution.attempts import RETRY_BACKOFF_S

    rt = await _boot(tmp_path, clock, runtime_cls, seconds=120)
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
    # Before retry_at: tick must not start a new attempt (Clock, not wall sleep).
    await rt.tick()
    assert rt.tasks["st_tr/SPEC"].state == TaskState.PENDING
    assert open_during_backoff == []
    clock.advance(timedelta(seconds=RETRY_BACKOFF_S))
    await rt.tick()
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
async def test_p3_retry_survives_crash_then_recover(tmp_path, clock, runtime_cls):
    """Pending retry_at is folded from the journal; tick fires it after Clock advance."""
    from coscientist.core.execution.attempts import RETRY_BACKOFF_S

    rt = await _boot(tmp_path, clock, runtime_cls, seconds=120)
    attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await rt.fail_attempt(aid, retryable=True)
    retry_at = rt._retry_at["st_tr/SPEC"]
    assert rt.tasks["st_tr/SPEC"].state == TaskState.PENDING
    await close_runtime(rt)

    # Reopen before retry_at: fold restores pending retry; tick must not fire yet.
    rt2 = await open_runtime(
        tmp_path, "st_tr", clock=clock, runtime_cls=runtime_cls
    )
    attach_executor(rt2, tmp_path)
    assert "st_tr/SPEC" in rt2._retry_at
    assert rt2._retry_at["st_tr/SPEC"] == retry_at
    assert rt2.tasks["st_tr/SPEC"].state == TaskState.PENDING
    await rt2.tick()
    assert rt2.tasks["st_tr/SPEC"].state == TaskState.PENDING

    clock.advance(timedelta(seconds=RETRY_BACKOFF_S))
    await rt2.tick()
    open_aids = [
        a
        for a in rt2.tasks["st_tr/SPEC"].attempts
        if not rt2.attempts.get(a, {}).get("closed")
    ]
    assert len(open_aids) == 1
    assert rt2.tasks["st_tr/SPEC"].state == TaskState.RUNNING
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_p3_retry_fires_exactly_once(tmp_path, clock, runtime_cls):
    """Due retry_at is removed from _retry_at when fired; a second tick does not re-start."""
    from coscientist.core.execution.attempts import RETRY_BACKOFF_S

    rt = await _boot(tmp_path, clock, runtime_cls, seconds=120)
    attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await rt.fail_attempt(aid, retryable=True)
    clock.advance(timedelta(seconds=RETRY_BACKOFF_S))
    await rt.tick()
    assert "st_tr/SPEC" not in rt._retry_at
    open1 = [
        a
        for a in rt.tasks["st_tr/SPEC"].attempts
        if not rt.attempts.get(a, {}).get("closed")
    ]
    assert len(open1) == 1
    n_attempts = len(rt.tasks["st_tr/SPEC"].attempts)
    await rt.tick()
    open2 = [
        a
        for a in rt.tasks["st_tr/SPEC"].attempts
        if not rt.attempts.get(a, {}).get("closed")
    ]
    assert open2 == open1
    assert len(rt.tasks["st_tr/SPEC"].attempts) == n_attempts
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_p5_deadline_fails_task(tmp_path, clock, runtime_cls):
    # Long study so SPEC.deadline (≤600s) is strictly before effective_deadline.
    rt = await _boot(tmp_path, clock, runtime_cls, seconds=3600, finalize_reserve_s=30)
    await rt.ensure_lease(ttl_s=10_000)
    await rt.start_attempt("SPEC")
    spec_dl = rt.tasks["st_tr/SPEC"].deadline
    assert spec_dl < rt.study.effective_deadline()
    # Past task deadline, still before study effective → P5 FAILED(deadline).
    clock.set(spec_dl + timedelta(seconds=1))
    await rt.tick()
    t = rt.tasks["st_tr/SPEC"]
    assert t.state == TaskState.DONE
    assert t.outcome == TaskOutcome.FAILED
    assert t.reason == "deadline"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_p6_cancel_skipped(tmp_path, clock, runtime_cls):
    rt = await _boot(tmp_path, clock, runtime_cls)
    await rt.cancel_task("SPEC", reason="cancelled")
    t = rt.tasks["st_tr/SPEC"]
    assert t.state == TaskState.DONE
    assert t.outcome == TaskOutcome.SKIPPED
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_deps_must_be_done(tmp_path, clock, runtime_cls):
    rt = await _boot(tmp_path, clock, runtime_cls)
    with pytest.raises(RuntimeError, match="dependencies"):
        await rt.start_attempt("HYP")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_recover_closes_open_attempt(tmp_path, clock, runtime_cls):
    rt = await _boot(tmp_path, clock, runtime_cls, seconds=120)
    await rt.start_attempt("SPEC")
    result = await recover(rt)
    assert result["closed"] >= 1
    assert result["retried"] >= 1
    t = rt.tasks["st_tr/SPEC"]
    # P3 retryable close → PENDING(+retry_at) until backoff starts the next attempt
    assert t.state in (TaskState.DONE, TaskState.RUNNING, TaskState.PENDING)
    assert rt.attempts[next(iter(rt.attempts))]["close_reason"] == "crashed"
    await close_runtime(rt)


def _spec_with_value_match(rt, aid: str) -> None:
    task = rt.tasks[rt.attempts[aid]["task_id"]]
    rt.tasks[task.task_id] = task.model_copy(
        update={
            "spec": task.spec.model_copy(
                update={"criteria": [RequirementRef(template_id="value_match")]}
            )
        }
    )


def _success_output(art_id: str, *, summary: str = "ok") -> AgentTaskOutput:
    return AgentTaskOutput(
        summary=summary,
        outputs={"main": ArtifactHandle(artifact_id=art_id, name="main")},
        values={"count": 225},
    )


@pytest.mark.asyncio
async def test_recover_records_from_cas_route_returned(tmp_path, clock, runtime_cls):
    """RouteReturned + CAS digest → recover rebuilds AgentTaskOutput (no invented summary)."""
    rt = await _boot(tmp_path, clock, runtime_cls, seconds=120)
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    art = arts.put(
        b'{"count": 225}',
        make_tool_ref(f"{aid}:1"),
        name="main",
        media_type="application/json",
    )
    _spec_with_value_match(rt, aid)
    out = _success_output(art.artifact_id, summary="from-agent")
    digest = await ex.mark_route_returned(aid, out)
    assert rt.attempts[aid]["route_returned"] is True
    assert rt.attempts[aid]["agent_output_digest"] == digest
    assert arts.read_cas(digest)

    # Simulate crash before TaskDone: recover must record from CAS, not invent output.
    result = await recover(rt)
    assert result["closed"] >= 1
    assert rt.tasks["st_tr/SPEC"].state == TaskState.DONE
    assert rt.tasks["st_tr/SPEC"].outcome == TaskOutcome.SUCCESS
    assert rt.attempts[aid]["closed"] is True
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_recover_missing_cas_crashes_retryable(tmp_path, clock, runtime_cls):
    """route_returned without CAS blob → crashed/retryable, no invented AgentTaskOutput."""
    rt = await _boot(tmp_path, clock, runtime_cls, seconds=120)
    attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await rt._append(
        [
            EventEnvelope(
                study_id="st_tr",
                type="RouteReturned",
                entity_key=aid,
                idem_key=make_idem("RouteReturned", aid),
                payload={"attempt_id": aid, "agent_output_digest": "missing" * 8},
                ts=clock.now(),
                schema_version=writer_schema_version('RouteReturned'),
            )
        ]
    )
    result = await recover(rt)
    assert result["closed"] >= 1
    assert result["retried"] >= 1
    assert rt.attempts[aid]["closed"] is True
    assert rt.attempts[aid]["close_reason"] == "crashed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_recover_corrupt_cas_crashes_retryable(tmp_path, clock, runtime_cls):
    """Corrupt CAS blob must not raise out of recover — close as crashed/retryable."""
    rt = await _boot(tmp_path, clock, runtime_cls, seconds=120)
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    art = arts.put(
        b'{"count": 225}',
        make_tool_ref(f"{aid}:1"),
        name="main",
        media_type="application/json",
    )
    _spec_with_value_match(rt, aid)
    digest = await ex.mark_route_returned(aid, _success_output(art.artifact_id))
    (arts.cas / digest).write_bytes(b"tampered-not-matching-sha256")

    result = await recover(rt)
    assert result["closed"] >= 1
    assert result["retried"] >= 1
    assert rt.attempts[aid]["closed"] is True
    assert rt.attempts[aid]["close_reason"] == "crashed"
    assert not any(e.type == "ResultRecorded" for e in await rt.store.read_all())
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_record_vs_deadline_race(tmp_path, clock, runtime_cls):
    """Stale in-memory attempt still open: store txn must reject record after deadline close."""
    rt = await _boot(tmp_path, clock, runtime_cls, seconds=30)
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    art = arts.put(
        b'{"count": 225}',
        make_tool_ref(f"{aid}:1"),
        name="main",
        media_type="application/json",
    )
    _spec_with_value_match(rt, aid)
    out = _success_output(art.artifact_id, summary="race")
    task = rt.tasks[rt.attempts[aid]["task_id"]]

    # Deadline wins first (journal + store projection), but leave in-memory attempt open
    # so Executor.record's early get_attempt() still sees closed=False — the old bug path.
    now = clock.now()
    await rt.writer.append_many(
        [
            EventEnvelope(
                study_id="st_tr",
                type="AttemptClosed",
                entity_key=aid,
                idem_key=make_idem("AttemptClosed", aid, "deadline"),
                payload={
                    "attempt_id": aid,
                    "closed": True,
                    "close_reason": "deadline",
                    "task_id": task.task_id,
                },
                ts=now,
                schema_version=writer_schema_version('AttemptClosed'),
            ),
            EventEnvelope(
                study_id="st_tr",
                type="TaskDone",
                entity_key=task.task_id,
                idem_key=make_idem("TaskDone", task.task_id),
                payload={
                    "task_id": task.task_id,
                    "outcome": TaskOutcome.FAILED.value,
                    "reason": "deadline",
                },
                ts=now,
                schema_version=writer_schema_version('TaskDone'),
            ),
        ]
    )
    assert (await rt.store.get_row("attempts", aid))["closed"] is True
    assert rt.attempts[aid].get("closed") is not True  # stale memory

    with pytest.raises(AttemptClosedError):
        await ex.record(aid, out)
    assert not any(e.type == "ResultRecorded" for e in await rt.store.read_all())
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_mark_route_returned_vs_deadline_race(tmp_path, clock, runtime_cls):
    """mark_route_returned also checks require_open_attempt inside the write txn."""
    rt = await _boot(tmp_path, clock, runtime_cls, seconds=30)
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    art = arts.put(
        b'{"count": 225}',
        make_tool_ref(f"{aid}:1"),
        name="main",
        media_type="application/json",
    )
    task = rt.tasks[rt.attempts[aid]["task_id"]]
    now = clock.now()
    await rt.writer.append_many(
        [
            EventEnvelope(
                study_id="st_tr",
                type="AttemptClosed",
                entity_key=aid,
                idem_key=make_idem("AttemptClosed", aid, "deadline"),
                payload={
                    "attempt_id": aid,
                    "closed": True,
                    "close_reason": "deadline",
                    "task_id": task.task_id,
                },
                ts=now,
                schema_version=writer_schema_version('AttemptClosed'),
            ),
        ]
    )
    assert rt.attempts[aid].get("closed") is not True  # stale memory
    with pytest.raises(AttemptClosedError):
        await ex.mark_route_returned(aid, _success_output(art.artifact_id))
    assert not any(e.type == "RouteReturned" for e in await rt.store.read_all())
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_repeated_record_after_success_raises_attempt_closed(tmp_path, clock, runtime_cls):
    """Second record after Result+Close+Done is AttemptClosedError, not Idempotent."""
    rt = await _boot(tmp_path, clock, runtime_cls, seconds=120)
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await record_success(rt, aid, arts)
    assert rt.attempts[aid]["closed"] is True
    with pytest.raises(AttemptClosedError):
        await ex.record(aid, _success_output("art_unused", summary="again"))
    results = [e for e in await rt.store.read_all() if e.type == "ResultRecorded"]
    assert len(results) == 1
    assert any(e.type == "LateWriteRejected" for e in await rt.store.read_all())
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_llm_hang_past_deadline(tmp_path, clock, runtime_cls):
    """Test double: cancel attempt work via P5 deadline (before study effective)."""
    rt = await _boot(tmp_path, clock, runtime_cls, seconds=3600, finalize_reserve_s=30)
    await rt.ensure_lease(ttl_s=10_000)
    aid = await rt.start_attempt("SPEC")

    async def _work():
        import asyncio

        try:
            await asyncio.sleep(10_000)
        except asyncio.CancelledError:
            return

    rt._attempt_tasks[aid] = asyncio.create_task(_work())
    spec_dl = rt.tasks["st_tr/SPEC"].deadline
    clock.set(spec_dl + timedelta(seconds=1))
    await rt.tick()
    t = rt.tasks["st_tr/SPEC"]
    assert t.state == TaskState.DONE
    assert t.outcome == TaskOutcome.FAILED
    assert t.reason == "deadline"
    assert rt.attempts[aid]["closed"] is True
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_creates_experiments(tmp_path, clock, runtime_cls):
    rt = await _boot(tmp_path, clock, runtime_cls, seconds=300)
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        aid = await rt.start_attempt(key)
        await record_success(rt, aid, arts)
    await rt.accept_plan([exp_spec("EXP-1"), exp_spec("EXP-2", ["EXP-1"])])
    assert "st_tr/EXP-1" in rt.tasks
    assert rt.tasks["st_tr/PLAN"].state == TaskState.DONE
    await close_runtime(rt)
