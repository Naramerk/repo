"""Build a StudyRuntime with store + writer bound to a data directory."""

from __future__ import annotations

from pathlib import Path

from coscientist.core.clock import Clock, SystemClock
from coscientist.core.graph import KindRegistry, default_execution_registry
from coscientist.core.machine import StudyRuntime
from coscientist.core.store import SqliteEventStore
from coscientist.core.writer import StudyWriter


async def open_runtime(
    data_root: Path | str,
    study_id: str,
    *,
    owner: str = "owner-1",
    clock: Clock | None = None,
    registry: KindRegistry | None = None,
    acquire: bool = True,
) -> StudyRuntime:
    clock = clock or SystemClock()
    root = Path(data_root) / study_id
    root.mkdir(parents=True, exist_ok=True)
    store = SqliteEventStore(study_id, root / "events.db", clock=clock)
    if acquire:
        await store.acquire_lease(owner)
    writer = StudyWriter(store)
    writer.start()
    rt = StudyRuntime(
        store,
        writer,
        clock=clock,
        registry=registry or default_execution_registry(),
        owner=owner,
    )
    # rebuild local projection from journal if any
    for ev in store.read_all():
        rt._project_local(ev)  # noqa: SLF001
    return rt


async def close_runtime(rt: StudyRuntime) -> None:
    await rt.writer.stop()
    rt.store.close()
