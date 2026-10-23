"""Lease acquire / release lifecycle for open_runtime and close_runtime."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.clock import FakeClock
from coscientist.core.errors import LeaseLost
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
    with pytest.raises(LeaseLost, match="epoch/nonce mismatch"):
        await store.release_lease("same-owner", epoch - 1)
    owner, live_epoch, _ = store.current_lease()
    assert owner == "same-owner"
    assert live_epoch == epoch
    await store.release_lease("same-owner", epoch)
    assert store.current_lease()[0] is None
    store.close()


@pytest.mark.asyncio
async def test_release_lease_rejects_foreign_owner(tmp_path, clock):
    store = SqliteEventStore("st_fo", tmp_path / "st_fo.db", clock=clock)
    epoch = await store.acquire_lease("owner-a", ttl_s=30)
    nonce = store.owner_nonce
    assert nonce
    with pytest.raises(LeaseLost, match="epoch/nonce mismatch"):
        await store.release_lease("owner-b", epoch, nonce=nonce)
    assert store.current_lease()[0] == "owner-a"
    await store.release_lease("owner-a", epoch, nonce=nonce)
    assert store.current_lease()[0] is None
    store.close()


@pytest.mark.asyncio
async def test_release_lease_rejects_foreign_nonce(tmp_path, clock):
    store = SqliteEventStore("st_fn", tmp_path / "st_fn.db", clock=clock)
    epoch = await store.acquire_lease("owner-a", ttl_s=30)
    with pytest.raises(LeaseLost, match="epoch/nonce mismatch"):
        await store.release_lease("owner-a", epoch, nonce="not-the-holder-nonce")
    assert store.current_lease()[0] == "owner-a"
    await store.release_lease("owner-a", epoch, nonce=store.owner_nonce)
    assert store.current_lease()[0] is None
    store.close()


@pytest.mark.asyncio
async def test_release_and_renew_raise_on_missing_nonce(tmp_path, clock):
    """release_lease and renew_lease share the same missing-nonce failure mode."""
    store = SqliteEventStore("st_mn", tmp_path / "st_mn.db", clock=clock)
    epoch = await store.acquire_lease("owner-a", ttl_s=30)
    store.owner_nonce = None
    with pytest.raises(LeaseLost, match="missing owner nonce"):
        await store.renew_lease("owner-a", epoch)
    with pytest.raises(LeaseLost, match="missing owner nonce"):
        await store.release_lease("owner-a", epoch)
    assert store.current_lease()[0] == "owner-a"
    store.close()


@pytest.mark.asyncio
async def test_same_owner_name_different_nonce_cannot_steal(tmp_path, clock):
    db = tmp_path / "st_steal.db"
    holder = SqliteEventStore("st_steal", db, clock=clock)
    epoch = await holder.acquire_lease("shared-name", ttl_s=30)
    assert epoch == 1
    interloper = SqliteEventStore("st_steal", db, clock=clock)
    with pytest.raises(LeaseLost, match="different nonce"):
        await interloper.acquire_lease("shared-name", ttl_s=30)
    with pytest.raises(LeaseLost):
        await interloper.renew_lease("shared-name", epoch, nonce="foreign-nonce")
    assert holder.current_lease()[0] == "shared-name"
    interloper.close()
    holder.close()


@pytest.mark.asyncio
async def test_migrated_live_lease_empty_nonce_waits_for_ttl(tmp_path, clock):
    """Old schema rows get owner_nonce=''; same owner cannot steal until TTL."""
    db = tmp_path / "st_mig.db"
    sid = "st_mig"
    expires = (clock.now() + timedelta(seconds=30)).isoformat()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE lease("
        "study_id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
        "epoch INT NOT NULL, expires_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO lease(study_id, owner, epoch, expires_at) VALUES (?,?,?,?)",
        (sid, "migrated-owner", 3, expires),
    )
    conn.commit()
    conn.close()

    store = SqliteEventStore(sid, db, clock=clock)
    cols = {
        str(r[1]) for r in store._conn.execute("PRAGMA table_info(lease)").fetchall()
    }
    assert "owner_nonce" in cols
    held = store._conn.execute(
        "SELECT owner_nonce, epoch FROM lease WHERE study_id=?", (sid,)
    ).fetchone()
    assert held["owner_nonce"] == ""
    assert int(held["epoch"]) == 3

    with pytest.raises(LeaseLost, match="different nonce"):
        await store.acquire_lease("migrated-owner", ttl_s=30)
    assert store.current_lease()[0] == "migrated-owner"

    clock.advance(timedelta(seconds=31))
    epoch = await store.acquire_lease("migrated-owner", ttl_s=30)
    assert epoch == 4
    assert store.owner_nonce
    store.close()


@pytest.mark.asyncio
async def test_store_close_is_idempotent(tmp_path, clock):
    store = SqliteEventStore("st_close2", tmp_path / "st_close2.db", clock=clock)
    assert store.closed is False
    store.close()
    assert store.closed is True
    store.close()  # second close must not raise
    assert store.closed is True


@pytest.mark.asyncio
async def test_open_runtime_cancelled_error_stops_writer_before_release(
    tmp_path, clock, monkeypatch
):
    """CancelledError after acquire must release; writer.stop before release_lease."""
    from datetime import timedelta

    from coscientist.core import runtime_factory as rf

    sid = "st_cancel"
    db = tmp_path / sid / "events.db"
    seed = await open_runtime(tmp_path, sid, clock=clock, owner="seeder")
    await seed.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid=sid,
    )
    await close_runtime(seed)

    order: list[str] = []
    stores: list[SqliteEventStore] = []
    writers: list[StudyWriter] = []

    class TrackingStore(SqliteEventStore):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            stores.append(self)

        async def release_lease(self, owner, epoch, *, nonce=None):
            order.append("release_lease")
            return await SqliteEventStore.release_lease(
                self, owner, epoch, nonce=nonce
            )

    class TrackingWriter(StudyWriter):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            writers.append(self)

        async def stop(self):
            order.append("writer_stop")
            return await StudyWriter.stop(self)

    monkeypatch.setattr(rf, "SqliteEventStore", TrackingStore)
    monkeypatch.setattr(rf, "StudyWriter", TrackingWriter)

    def boom_project(self, ev):
        raise asyncio.CancelledError()

    monkeypatch.setattr(StudyRuntime, "_project_local", boom_project)

    with pytest.raises(asyncio.CancelledError):
        await open_runtime(tmp_path, sid, clock=clock, owner="owner-cancel")

    assert _lease_owner(db, sid, clock) is None
    assert stores and stores[-1].closed
    assert writers and writers[-1].running is False
    assert order == ["writer_stop", "release_lease"]


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

    stores: list[SqliteEventStore] = []
    writers: list[StudyWriter] = []

    class TrackingStore(SqliteEventStore):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            stores.append(self)

    class TrackingWriter(StudyWriter):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            writers.append(self)

    monkeypatch.setattr(rf, "SqliteEventStore", TrackingStore)
    monkeypatch.setattr(rf, "StudyWriter", TrackingWriter)

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

        class BoomWriter(TrackingWriter):
            def __init__(self, *a, **k):
                raise RuntimeError("inject:writer_init")

        monkeypatch.setattr(rf, "StudyWriter", BoomWriter)

    elif fail_at == "writer_start":

        def boom_start(self):
            raise RuntimeError("inject:writer_start")

        monkeypatch.setattr(TrackingWriter, "start", boom_start)

    elif fail_at == "runtime_cls":

        class BoomRuntime(StudyRuntime):
            def __init__(self, *a, **k):
                raise RuntimeError("inject:runtime_cls")

        with pytest.raises(RuntimeError, match="inject:runtime_cls"):
            await open_runtime(
                tmp_path, sid, clock=clock, owner=owner, runtime_cls=BoomRuntime
            )
        assert _lease_owner(db, sid, clock) is None
        assert stores, "open_runtime must construct a store"
        assert stores[-1].closed
        assert writers, "writer starts before runtime_cls"
        assert writers[-1].running is False
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
    assert stores, "open_runtime must construct a store"
    assert stores[-1].closed
    if fail_at in ("runtime_cls", "project_local"):
        assert writers and writers[-1].running is False
    elif fail_at == "writer_start":
        # start() raised before the writer was marked running.
        assert writers and writers[-1].running is False
    # Prove the lease is free without going through the still-patched open_runtime.
    store = SqliteEventStore(sid, db, clock=clock)
    await store.acquire_lease("reopen")
    assert store.current_lease()[0] == "reopen"
    await store.release_lease("reopen", store.epoch)
    store.close()
