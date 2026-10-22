"""Build a StudyRuntime with store + writer bound to a data directory."""

from __future__ import annotations

from pathlib import Path

from coscientist.core.clock import Clock, SystemClock
from coscientist.core.graph import KindRegistry, default_execution_registry
from coscientist.core.hitl_guard import reject_open_hitl_if_unsupported
from coscientist.core.machine import StudyRuntime, new_owner
from coscientist.core.store import SqliteEventStore
from coscientist.core.writer import StudyWriter


async def open_runtime(
    data_root: Path | str,
    study_id: str,
    *,
    owner: str | None = None,
    clock: Clock | None = None,
    registry: KindRegistry | None = None,
    acquire: bool = True,
    runtime_cls: type[StudyRuntime] | None = None,
) -> StudyRuntime:
    """Open a study runtime. ``runtime_cls`` defaults to core ``StudyRuntime``.

    Core refuses journals that still have open HITL gates (use experimental).
    """
    cls = runtime_cls or StudyRuntime
    clock = clock or SystemClock()
    owner = owner or new_owner()
    root = Path(data_root) / study_id
    root.mkdir(parents=True, exist_ok=True)
    store = SqliteEventStore(study_id, root / "events.db", clock=clock, owner=None)
    if acquire:
        await store.acquire_lease(owner)
    writer = StudyWriter(store)
    writer.start()
    rt = cls(
        store,
        writer,
        clock=clock,
        registry=registry or default_execution_registry(),
        owner=owner,
    )
    events = store.read_all()
    try:
        reject_open_hitl_if_unsupported(rt, events)
    except Exception:
        await close_runtime(rt)
        raise
    for ev in events:
        rt._project_local(ev)  # noqa: SLF001
    return rt


async def close_runtime(rt: StudyRuntime) -> None:
    for t in list(rt._retry_tasks.values()):  # noqa: SLF001
        t.cancel()
    for t in list(rt._attempt_tasks.values()):  # noqa: SLF001
        t.cancel()
    await rt.writer.stop()
    rt.store.close()
