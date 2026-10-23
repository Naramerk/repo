"""Lease acquire / release lifecycle for open_runtime and close_runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from coscientist.core.clock import FakeClock
from coscientist.core.machine import StudyRuntime
from coscientist.core.runtime_factory import close_runtime, open_runtime
from coscientist.core.store import SqliteEventStore
from coscientist.core.writer import StudyWriter


def _lease_owner(db: Path, study_id: str, clock: FakeClock) -> str | None:
    store = SqliteEventStore(study_id, db, clock=clock)
    owner, _epoch, _exp = store.current_lease()
    store.close()
    return owner


@pytest.mark.asyncio
async def test_close_runtime_releases_lease(tmp_path, clock):
    rt = await open_runtime(tmp_path, "st_close", clock=clock, owner="owner-a")
    db = tmp_path / "st_close" / "events.db"
    assert _lease_owner(db, "st_close", clock) == "owner-a"
    await close_runtime(rt)
    assert _lease_owner(db, "st_close", clock) is None
    # Another owner can acquire immediately without waiting for TTL.
    rt2 = await open_runtime(tmp_path, "st_close", clock=clock, owner="owner-b")
    assert rt2.owner == "owner-b"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_release_lease_requires_matching_epoch(tmp_path, clock):
    store = SqliteEventStore("st_ep", tmp_path / "st_ep.db", clock=clock)
    epoch = await store.acquire_lease("same-owner", ttl_s=30)
    # Stale epoch must not delete the live lease.
    await store.release_lease("same-owner", epoch - 1)
    owner, live_epoch, _ = store.current_lease()
    assert owner == "same-owner"
    assert live_epoch == epoch
    await store.release_lease("same-owner", epoch)
    assert store.current_lease()[0] is None
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fail_at",
    ["hitl_check", "writer_init", "writer_start", "runtime_cls", "project_local"],
)
async def test_open_runtime_releases_lease_on_post_acquire_failure(
    tmp_path, clock, monkeypatch, fail_at
):
    """Any failure after acquire_lease must release the lease and close the store."""
    from datetime import timedelta

    from coscientist.core import hitl_guard
    from coscientist.core import runtime_factory as rf

    sid = f"st_fail_{fail_at}"
    db = tmp_path / sid / "events.db"
    owner = f"owner-{fail_at}"

    # Seed a journal so _project_local has work (and reopen paths are realistic).
    seed = await open_runtime(tmp_path, sid, clock=clock, owner="seeder")
    await seed.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid=sid,
    )
    await close_runtime(seed)
    assert _lease_owner(db, sid, clock) is None

    if fail_at == "hitl_check":
        # Pre-lease check must pass; fail only on the post-lease re-check.
        calls = {"n": 0}
        real_reject = hitl_guard.reject_open_hitl_if_unsupported

        def reject_second(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("inject:hitl_check")
            return real_reject(*a, **k)

        monkeypatch.setattr(rf, "reject_open_hitl_if_unsupported", reject_second)

    elif fail_at == "writer_init":

        class BoomWriter(StudyWriter):
            def __init__(self, *a, **k):
                raise RuntimeError("inject:writer_init")

        monkeypatch.setattr(rf, "StudyWriter", BoomWriter)

    elif fail_at == "writer_start":

        def boom_start(self):
            raise RuntimeError("inject:writer_start")

        monkeypatch.setattr(StudyWriter, "start", boom_start)

    elif fail_at == "runtime_cls":

        class BoomRuntime(StudyRuntime):
            def __init__(self, *a, **k):
                raise RuntimeError("inject:runtime_cls")

        with pytest.raises(RuntimeError, match="inject:runtime_cls"):
            await open_runtime(
                tmp_path, sid, clock=clock, owner=owner, runtime_cls=BoomRuntime
            )
        assert _lease_owner(db, sid, clock) is None
        store = SqliteEventStore(sid, db, clock=clock)
        await store.acquire_lease("reopen")
        assert store.current_lease()[0] == "reopen"
        await store.release_lease("reopen", store.epoch)
        store.close()
        return

    elif fail_at == "project_local":

        def boom_project(self, ev):
            raise RuntimeError("inject:project_local")

        monkeypatch.setattr(StudyRuntime, "_project_local", boom_project)

    with pytest.raises(RuntimeError, match="inject:"):
        await open_runtime(tmp_path, sid, clock=clock, owner=owner)

    assert _lease_owner(db, sid, clock) is None
    # Prove the lease is free without going through the still-patched open_runtime.
    store = SqliteEventStore(sid, db, clock=clock)
    await store.acquire_lease("reopen")
    assert store.current_lease()[0] == "reopen"
    await store.release_lease("reopen", store.epoch)
    store.close()
