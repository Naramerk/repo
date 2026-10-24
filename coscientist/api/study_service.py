"""Filesystem-backed ``StudyService`` (DESIGN §9). Raises core ``StudyNotFound``."""

from __future__ import annotations

import asyncio
from pathlib import Path

from coscientist.api.errors import StudyIdRefuse, StudyViewLoopRefuse
from coscientist.api.runtime import StudyRuntime as ApiStudyRuntime
from coscientist.api.views import require_study_id
from coscientist.core.events.errors import STUDY_NOT_FOUND_REASON, StudyNotFound
from coscientist.core.execution.domain import Catalog, StudyView
from coscientist.core.execution.runtime_factory import close_runtime, open_runtime


def _absent_loop() -> asyncio.AbstractEventLoop | None:
    """Sentinel for ``except RuntimeError`` (Call assign — not a silent default)."""
    return None


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
        # Detect loop first; run work outside except (no __context__ entanglement).
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = _absent_loop()
        if loop is not None:
            raise StudyViewLoopRefuse("study_view_requires_no_running_event_loop")
        return asyncio.run(self._study_view_async(study_id))

    async def _study_view_async(self, study_id: str) -> StudyView:
        sid, db_path = study_events_db(self._root, study_id)
        if not db_path.is_file():
            raise StudyNotFound(sid, reason=STUDY_NOT_FOUND_REASON)
        # Journal exists — open_runtime may mkdir; never reached for missing studies.
        rt = await open_runtime(self._root, sid, catalog=self._catalog, runtime_cls=ApiStudyRuntime)
        try:
            if rt.study is None:
                raise StudyNotFound(sid, reason=STUDY_NOT_FOUND_REASON)
            return rt.view()
        finally:
            await close_runtime(rt)
