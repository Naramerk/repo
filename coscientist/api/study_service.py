"""Filesystem-backed ``StudyService`` (DESIGN §9). Raises core ``StudyNotFound``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from coscientist.api.errors import StudyIdRefuse, StudyViewLoopRefuse
from coscientist.api.runtime import StudyRuntime as ApiStudyRuntime
from coscientist.api.views import require_study_id
from coscientist.core.events.clock import Clock
from coscientist.core.events.errors import STUDY_NOT_FOUND_REASON, StudyNotFound
from coscientist.core.events.ports import EventStore
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.execution.domain import Catalog, StudyView
from coscientist.core.execution.machine import StudyRuntime
from coscientist.core.execution.runtime_factory import close_runtime, open_runtime


def _has_running_loop() -> bool:
    """True iff ``asyncio.get_running_loop()`` succeeds."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False  # audit-ok[except_silent_return]: documented no-loop signal from get_running_loop


def study_events_db(data_root: Path, study_id: object) -> tuple[str, Path]:
    """Resolve ``data_root/<id>/events.db``; refuse ids that escape the root."""
    sid = require_study_id(study_id)
    root = Path(data_root).resolve()
    db_path = (root / sid / "events.db").resolve()
    try:
        db_path.relative_to(root)
    except ValueError:
        raise StudyIdRefuse("study_id_escapes_data_root") from None
    return sid, db_path


def bind_event_store_for(
    data_root: Path | str,
    *,
    clock: Clock,
) -> Callable[[str], EventStore]:
    """Composition-root adapter: ``AppDeps.event_store_for`` → SQLite journal.

    Missing ``events.db`` → ``StudyNotFound`` (no mkdir). ``clock`` required —
    no silent ``SystemClock`` at this seam.
    """
    root = Path(data_root)

    def event_store_for(study_id: str) -> EventStore:
        sid, db_path = study_events_db(root, study_id)
        if not db_path.is_file():
            raise StudyNotFound(sid, reason=STUDY_NOT_FOUND_REASON)
        return SqliteEventStore(sid, db_path, clock=clock, owner=None)

    return event_store_for


@asynccontextmanager
async def open_study_runtime(
    data_root: Path | str,
    study_id: object,
    *,
    catalog: Catalog,
) -> AsyncIterator[tuple[str, StudyRuntime]]:
    """Open journal runtime; missing DB / empty study → ``StudyNotFound``."""
    root = Path(data_root)
    sid, db_path = study_events_db(root, study_id)
    if not db_path.is_file():
        raise StudyNotFound(sid, reason=STUDY_NOT_FOUND_REASON)
    rt = await open_runtime(root, sid, catalog=catalog, runtime_cls=ApiStudyRuntime)
    try:
        if rt.study is None:
            raise StudyNotFound(sid, reason=STUDY_NOT_FOUND_REASON)
        yield sid, rt
    finally:
        await close_runtime(rt)


class DataRootStudyService:
    """Open study journals under ``data_root``; missing DB → ``StudyNotFound``.

    ``study_view`` is sync (port contract). Call from a thread without a running
    event loop (``asyncio.run``); composition roots that already hold a loop
    should use an async adapter instead of this class.
    """

    def __init__(self, data_root: Path | str, *, catalog: Catalog) -> None:
        self._root = Path(data_root)
        self._catalog = catalog

    def study_view(self, study_id: str) -> StudyView:
        if _has_running_loop():
            raise StudyViewLoopRefuse("study_view_requires_no_running_event_loop")
        return asyncio.run(self._study_view_async(study_id))

    async def _study_view_async(self, study_id: str) -> StudyView:
        async with open_study_runtime(self._root, study_id, catalog=self._catalog) as (_sid, rt):
            return rt.view()
