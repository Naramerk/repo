"""Build a StudyRuntime with store + writer bound to a data directory."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from coscientist.core.artifacts import LocalArtifactStore
from coscientist.core.clock import Clock, SystemClock
from coscientist.core.errors import LeaseLost
from coscientist.core.graph import KindRegistry, default_execution_registry
from coscientist.core.hitl_guard import reject_open_hitl_if_unsupported
from coscientist.core.machine import StudyRuntime, new_owner
from coscientist.core.store import SqliteEventStore
from coscientist.core.writer import StudyWriter

log = logging.getLogger(__name__)


async def open_runtime(
    data_root: Path | str,
    study_id: str,
    *,
    catalog: Any,
    owner: str | None = None,
    clock: Clock | None = None,
    registry: KindRegistry | None = None,
    acquire: bool = True,
    runtime_cls: type[StudyRuntime] | None = None,
    artifacts: Any | None = None,
) -> StudyRuntime:
    """Open a study runtime. ``runtime_cls`` defaults to core ``StudyRuntime``.

    ``catalog`` is **required** (production callers pass a real catalog; tests
    use ``minimal_test_catalog`` via ``tests.helpers.open_runtime``). Test
    helpers are never imported from this module.

    Core refuses journals that still have open HITL gates (use experimental).

    A cheap pre-lease HITL check is a fast path only. After ``acquire_lease``,
    the journal is re-read and that post-lease snapshot is the sole source for
    HITL re-check and local rebuild (closes the TOCTOU window).

    Acquire runs under ``asyncio.shield``: on cancel we wait for the worker
    thread to finish, then release if this open acquired the lease (not merely
    if ``owner_nonce`` was seeded on the constructor with ``acquire=False``).

    Any failure after the lease is acquired stops a started writer, releases the
    lease (owner+epoch+nonce), and closes the store. Cleanup catches
    ``BaseException`` so ``CancelledError`` cannot leave the lease held.
    """
    if catalog is None:
        raise TypeError("open_runtime requires catalog= (no test-catalog fallback)")
    cls = runtime_cls or StudyRuntime
    clock = clock or SystemClock()
    owner = owner or new_owner()
    root = Path(data_root) / study_id
    root.mkdir(parents=True, exist_ok=True)
    store = SqliteEventStore(study_id, root / "events.db", clock=clock, owner=None)

    # Fast path: refuse before taking the lease when the journal is already dirty.
    try:
        reject_open_hitl_if_unsupported(cls, await store.read_all())
    except BaseException:
        try:
            await store.close()
        except BaseException:
            log.warning("store.close failed during pre-lease cleanup", exc_info=True)
        raise

    writer: StudyWriter | None = None
    writer_started = False
    # Only release leases this open_runtime call actually acquired (not seeded nonce).
    acquired_here = False
    try:
        if acquire:
            task = asyncio.create_task(store.acquire_lease(owner))
            try:
                await asyncio.shield(task)
                acquired_here = True
            except asyncio.CancelledError:
                # Wait for the worker thread; it may have committed before cancel.
                try:
                    await task
                except BaseException:
                    pass
                acquired_here = store.owner == owner and bool(store.owner_nonce)
                raise

        # Authoritative read under the lease — never rebuild from the pre-lease snapshot.
        events = await store.read_all()
        reject_open_hitl_if_unsupported(cls, events)

        writer = StudyWriter(store)
        writer.start()
        writer_started = True
        rt = cls(
            store,
            writer,
            clock=clock,
            registry=registry or default_execution_registry(),
            owner=owner,
        )
        for ev in events:
            rt._project_local(ev)  # noqa: SLF001
        # Production path: Executor always via make_executor (live get_graph).
        arts = artifacts if artifacts is not None else LocalArtifactStore(root / "arts")
        rt.make_executor(catalog, arts)
        return rt
    except BaseException:
        # Stop the writer first so in-flight appends cannot race release.
        if writer_started and writer is not None:
            try:
                await writer.stop()
            except BaseException:
                log.warning("writer.stop failed during open_runtime cleanup", exc_info=True)
        if acquired_here:
            try:
                await store.release_lease(owner, store.epoch)
            except BaseException:
                log.warning(
                    "release_lease failed during open_runtime cleanup", exc_info=True
                )
        try:
            await store.close()
        except BaseException:
            log.warning("store.close failed during open_runtime cleanup", exc_info=True)
        raise


async def close_runtime(rt: StudyRuntime) -> None:
    for t in list(rt._attempt_tasks.values()):  # noqa: SLF001
        t.cancel()
    try:
        await rt.writer.stop()
    finally:
        try:
            if rt.store.owner is not None:
                await rt.store.release_lease(rt.owner, rt.store.epoch)
        except LeaseLost:
            log.warning("release_lease during close_runtime", exc_info=True)
        finally:
            await rt.store.close()
