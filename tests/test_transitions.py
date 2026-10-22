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
    assert result["retried"] >= 1
    t = rt.tasks["st_tr/SPEC"]
    # P3 retryable close → PENDING(+retry_at) until backoff starts the next attempt
    assert t.state in (TaskState.DONE, TaskState.RUNNING, TaskState.PENDING)
    assert rt.attempts[next(iter(rt.attempts))]["close_reason"] == "crashed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_recover_records_from_cas_route_returned(tmp_path, clock):
    """RouteReturned + CAS digest → recover rebuilds AgentTaskOutput (no invented summary)."""
    from coscientist.core.domain import AgentTaskOutput, ArtifactHandle, RequirementRef

    rt = await _boot(tmp_path, clock, seconds=120)
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    content = b'{"count": 225}'
    art = arts.put(
        content,
        __import__("coscientist.core.artifacts", fromlist=["make_tool_ref"]).make_tool_ref(
            f"{aid}:1"
        ),
        name="main",
        media_type="application/json",
    )
    task = rt.tasks[rt.attempts[aid]["task_id"]]
    rt.tasks[task.task_id] = task.model_copy(
        update={
            "spec": task.spec.model_copy(
                update={"criteria": [RequirementRef(template_id="value_match")]}
            )
        }
    )
    out = AgentTaskOutput(
        summary="from-agent",
        outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        values={"count": 225},
    )
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
async def test_recover_missing_cas_crashes_retryable(tmp_path, clock):
    """route_returned without CAS blob → crashed/retryable, no invented AgentTaskOutput."""
    from coscientist.core.events import EventEnvelope, make_idem

    rt = await _boot(tmp_path, clock, seconds=120)
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
async def test_record_vs_deadline_race(tmp_path, clock):
    """Stale in-memory attempt still open: store txn must reject record after deadline close."""
    from coscientist.core.artifacts import make_tool_ref
    from coscientist.core.domain import AgentTaskOutput, ArtifactHandle, RequirementRef
    from coscientist.core.errors import AttemptClosedError
    from coscientist.core.events import EventEnvelope, make_idem

    rt = await _boot(tmp_path, clock, seconds=30)
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    art = arts.put(
        b'{"count": 225}',
        make_tool_ref(f"{aid}:1"),
        name="main",
        media_type="application/json",
    )
    task = rt.tasks[rt.attempts[aid]["task_id"]]
    rt.tasks[task.task_id] = task.model_copy(
        update={
            "spec": task.spec.model_copy(
                update={"criteria": [RequirementRef(template_id="value_match")]}
            )
        }
    )
    out = AgentTaskOutput(
        summary="race",
        outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        values={"count": 225},
    )

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
            ),
        ]
    )
    assert rt.store.get_row("attempts", aid)["closed"] is True
    assert rt.attempts[aid].get("closed") is not True  # stale memory

    with pytest.raises(AttemptClosedError):
        await ex.record(aid, out)
    assert not any(e.type == "ResultRecorded" for e in rt.store.read_all())
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
