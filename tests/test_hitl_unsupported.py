"""Core must refuse journals that still have open HITL gates."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.events.errors import HitlUnsupported
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.execution.domain import GateConfig, HitlPolicy
from coscientist.core.execution.machine import StudyRuntime as CoreStudyRuntime
from coscientist.core.execution.recover import recover
from coscientist.experimental.runtime import StudyRuntime as ExpStudyRuntime
from tests.helpers import attach_executor, close_runtime, open_runtime, record_success


async def _boot_exp(tmp_path: Path, clock, sid: str = "st_hitl", **study_kw):
    root = tmp_path / sid
    rt = await open_runtime(
        root, sid, clock=clock, runtime_cls=ExpStudyRuntime, owner="owner-hitl"
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(minutes=30),
        sid=sid,
        **study_kw,
    )
    _, arts = attach_executor(rt, root)
    await record_success(rt, await rt.start_attempt("SPEC"), arts)
    return root, rt


async def _close_and_expire(rt, clock) -> str:
    owner = rt.owner
    await close_runtime(rt)
    clock.advance(timedelta(seconds=60))
    return owner


@pytest.mark.asyncio
async def test_core_open_runtime_rejects_open_hitl_journal(tmp_path, clock):
    root, rt = await _boot_exp(tmp_path, clock)
    await rt.open_hitl("PLAN", kind="plan_approval", timeout_s=60)
    assert any(h.open for h in rt.hitl.values())
    await _close_and_expire(rt, clock)
    with pytest.raises(HitlUnsupported, match="open HITL"):
        await open_runtime(
            root, "st_hitl", clock=clock, runtime_cls=CoreStudyRuntime, owner="core-o"
        )


@pytest.mark.asyncio
async def test_core_recover_rejects_open_hitl_journal(tmp_path, clock):
    root, rt = await _boot_exp(tmp_path, clock)
    await rt.open_hitl("PLAN", kind="plan_approval", timeout_s=60)
    owner = await _close_and_expire(rt, clock)
    exp = await open_runtime(
        root, "st_hitl", clock=clock, runtime_cls=ExpStudyRuntime, owner=owner
    )
    core = CoreStudyRuntime(exp.store, exp.writer, clock=clock, owner=exp.owner)
    for ev in await exp.store.read_all():
        core._project_local(ev)  # noqa: SLF001
    with pytest.raises(HitlUnsupported, match="open HITL"):
        await recover(core)
    await close_runtime(exp)


@pytest.mark.asyncio
async def test_experimental_open_runtime_accepts_open_hitl_journal(tmp_path, clock):
    root, rt = await _boot_exp(tmp_path, clock)
    await rt.open_hitl("PLAN", kind="plan_approval", timeout_s=60)
    owner = await _close_and_expire(rt, clock)
    rt2 = await open_runtime(
        root, "st_hitl", clock=clock, runtime_cls=ExpStudyRuntime, owner=owner
    )
    assert any(h.open for h in rt2.hitl.values())
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_core_opens_when_all_hitl_gates_closed(tmp_path, clock):
    """Approve (or timeout-close) every gate → core open + recover succeed."""
    from coscientist.core.execution.domain import TaskState

    root, rt = await _boot_exp(tmp_path, clock)
    rid = await rt.open_hitl("PLAN", kind="plan_approval", timeout_s=60)
    await rt.answer_hitl(rid, "accept")
    assert rt.hitl[rid].open is False
    owner = await _close_and_expire(rt, clock)

    core = await open_runtime(
        root, "st_hitl", clock=clock, runtime_cls=CoreStudyRuntime, owner=owner
    )
    # SPEC already DONE via record_success; no open attempts to close.
    assert core.tasks["st_hitl/SPEC"].state == TaskState.DONE
    out = await recover(core)
    assert out == {
        "closed": 0,
        "retried": 0,
        "expansions_resumed": 0,
        "foreach_resumed": 0,
        "loops_resumed": 0,
    }
    assert core.tasks["st_hitl/SPEC"].state == TaskState.DONE
    await close_runtime(core)


@pytest.mark.asyncio
async def test_core_rejects_wait_keep_open_hitl_gate(tmp_path, clock):
    """WAIT / keep_open leaves the gate open → HitlUnsupported for core."""
    policy = HitlPolicy(
        mode="interactive",
        gates={
            "costly_or_nonreal": GateConfig(
                enabled=True, timeout_s=30, on_timeout="fail"
            )
        },
    )
    root, rt = await _boot_exp(tmp_path, clock, hitl_policy=policy)
    # No Budget(delegated_costly) → «реши сам» yields WAIT / keep_open.
    rid = await rt.open_hitl("PLAN", kind="costly_or_nonreal", cost_rub=100)
    await rt.answer_hitl(rid, "delegate")
    assert rt.hitl[rid].open is True
    await _close_and_expire(rt, clock)

    with pytest.raises(HitlUnsupported, match="open HITL"):
        await open_runtime(
            root, "st_hitl", clock=clock, runtime_cls=CoreStudyRuntime, owner="core-w"
        )


@pytest.mark.asyncio
async def test_hitl_unsupported_does_not_hold_lease(tmp_path, clock):
    """Refused core open must not acquire a lease (journal checked first)."""
    root, rt = await _boot_exp(tmp_path, clock, sid="st_lease")
    await rt.open_hitl("PLAN", kind="plan_approval", timeout_s=60)
    await _close_and_expire(rt, clock)

    with pytest.raises(HitlUnsupported):
        await open_runtime(
            root, "st_lease", clock=clock, runtime_cls=CoreStudyRuntime, owner="core-x"
        )

    # No lease row held by core-x; experimental can acquire immediately.
    store = SqliteEventStore("st_lease", root / "st_lease" / "events.db", clock=clock)
    owner, epoch, expires = await store.current_lease()
    await store.close()
    assert owner != "core-x"
    # And a fresh experimental open with a new owner succeeds without clock advance.
    exp = await open_runtime(
        root, "st_lease", clock=clock, runtime_cls=ExpStudyRuntime, owner="exp-y"
    )
    assert exp.owner == "exp-y"
    await close_runtime(exp)


@pytest.mark.asyncio
async def test_open_runtime_post_lease_reread_toctou(tmp_path, clock, monkeypatch):
    """Events appended between pre-check and lease are visible after acquire.

    Inject HitlOpened + an ordinary TaskDone under a racing writer before the
    opener's acquire_lease. Core must raise HitlUnsupported and release; the
    experimental path must project both new events.
    """
    from coscientist.core.events.events import UnstampedEvent, make_idem
    from coscientist.core.events.ids import hitl_request_id
    from coscientist.core.execution.domain import TaskOutcome, TaskState

    root, rt = await _boot_exp(tmp_path, clock, sid="st_race")
    n_before = len(await rt.store.read_all())
    owner = await _close_and_expire(rt, clock)
    db_path = root / "st_race" / "events.db"

    real_acquire = SqliteEventStore.acquire_lease
    injected = {"done": False}

    async def acquire_after_race(self, acq_owner: str, ttl_s: float | None = None):
        if not injected["done"]:
            injected["done"] = True
            # Inject under a separate connection using the unbound method (not the patch).
            racer = SqliteEventStore("st_race", db_path, clock=clock)
            await real_acquire(racer, "racer")
            now = clock.now()
            rid = hitl_request_id()
            hitl_payload = {
                "request_id": rid,
                "kind": "plan_approval",
                "payload_ref": "st_race/PLAN",
                "deadline": (now + timedelta(seconds=30)).isoformat(),
                "study_id": "st_race",
                "task_id": "st_race/PLAN",
                "open": True,
                "cost_rub": 0.0,
            }
            await racer.append_many(
                [
                    UnstampedEvent(
                        study_id="st_race",
                        type="HitlOpened",
                        entity_key=rid,
                        idem_key=make_idem("HitlOpened", rid),
                        payload=hitl_payload,
                        ts=now,
                    ),
                    UnstampedEvent(
                        study_id="st_race",
                        type="TaskDone",
                        entity_key="st_race/REPORT",
                        idem_key=make_idem("TaskDone", "st_race/REPORT", "race"),
                        payload={
                            "task_id": "st_race/REPORT",
                            "outcome": TaskOutcome.SKIPPED.value,
                            "reason": "cancelled",
                        },
                        ts=now,
                        schema_version=1,
                    ),
                ]
            )
            await racer.release_lease("racer", racer.epoch)
            await racer.close()
        return await real_acquire(self, acq_owner, ttl_s=ttl_s)

    monkeypatch.setattr(SqliteEventStore, "acquire_lease", acquire_after_race)

    with pytest.raises(HitlUnsupported, match="open HITL"):
        await open_runtime(
            root, "st_race", clock=clock, runtime_cls=CoreStudyRuntime, owner="core-race"
        )
    store = SqliteEventStore("st_race", db_path, clock=clock)
    held_owner, _, _ = await store.current_lease()
    await store.close()
    assert held_owner != "core-race"

    # Experimental accepts the raced journal and rebuilds from the post-lease read.
    exp = await open_runtime(
        root, "st_race", clock=clock, runtime_cls=ExpStudyRuntime, owner=owner
    )
    events = await exp.store.read_all()
    assert len(events) == n_before + 2
    assert any(e.type == "HitlOpened" for e in events)
    assert any(
        e.type == "TaskDone" and e.payload.get("reason") == "cancelled" for e in events
    )
    assert exp.tasks["st_race/REPORT"].state == TaskState.DONE
    assert exp.tasks["st_race/REPORT"].reason == "cancelled"
    assert any(h.open for h in exp.hitl.values())
    await close_runtime(exp)
