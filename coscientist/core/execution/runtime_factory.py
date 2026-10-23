"""Build a StudyRuntime with store + writer bound to a data directory."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from coscientist.core.claims.graph import KindRegistry, default_execution_registry
from coscientist.core.events.clock import Clock, SystemClock
from coscientist.core.events.errors import LeaseLost
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.hitl_guard import reject_open_hitl_if_unsupported
from coscientist.core.execution.machine import StudyRuntime, new_owner
from coscientist.core.execution.tool_loader import ensure_core_tools_ready

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
        # PR13: rebuild judge state from journal so reopen == live.
        from coscientist.core.claims.reviews import (
            JUDGE_CAS_CORRUPT_LINE,
            JudgePendingCorrupt,
            apply_judge_journal,
        )

        try:
            apply_judge_journal(rt.executor, events)
        except JudgePendingCorrupt as e:
            # Corrupt/missing pending CAS → honest internal-error finalize (no hang).
            from coscientist.core.claims.reasons import TaskReason
            from coscientist.core.events.events import EventEnvelope, make_idem
            from coscientist.core.execution.domain import TaskOutcome, TaskState

            now = rt.clock.now()
            sid = rt.study.study_id if rt.study else study_id
            close_events: list[Any] = [
                EventEnvelope(
                    study_id=sid,
                    type="JudgeCasCorrupt",
                    entity_key=sid,
                    idem_key=make_idem("JudgeCasCorrupt", sid, str(e)[:80]),
                    payload={
                        "detail": str(e),
                        "report_line": JUDGE_CAS_CORRUPT_LINE,
                    },
                    ts=now,
                )
            ]
            for t in list(rt.tasks.values()):
                if getattr(t, "state", None) == TaskState.DONE:
                    continue
                close_events.append(
                    EventEnvelope(
                        study_id=sid,
                        type="TaskDone",
                        entity_key=t.task_id,
                        idem_key=make_idem("TaskDone", t.task_id, "judge_cas_corrupt"),
                        payload={
                            "task_id": t.task_id,
                            "outcome": TaskOutcome.FAILED.value,
                            "reason": TaskReason.CORE_BUG.value,
                            "detail": str(e),
                        },
                        ts=now,
                        schema_version=2,
                    )
                )
            await rt._append(close_events)  # noqa: SLF001
            import coscientist.core.finalize.finalize as finalize_mod

            await finalize_mod.finalize(rt, trigger="internal_error")
        # PR18: seal tool registry after built-ins — no late register_tool at runtime.
        ensure_core_tools_ready()
        # Open (and finalize) may journal FetchIntegrityRaised; executor reads must not.
        # Never append after StudyFinalized (store would raise StudyClosedError).
        if not getattr(rt, "_finalized", False):
            try:
                from coscientist.core.execution.source_fetch import reconcile_fetch_integrity

                await reconcile_fetch_integrity(writer)
            except LeaseLost:
                raise
            except Exception:  # noqa: BLE001 — open must stay total
                log.warning("reconcile_fetch_integrity on open failed", exc_info=True)
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
                log.warning("release_lease failed during open_runtime cleanup", exc_info=True)
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
