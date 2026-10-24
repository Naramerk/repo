"""Filesystem-backed ``StudyService`` (DESIGN §9). Raises core ``StudyNotFound``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from coscientist.core.events.errors import StudyNotFound
from coscientist.core.execution.domain import StudyView


class DataRootStudyService:
    """Open study journals under ``data_root``; missing DB → ``StudyNotFound``.

    ``study_view`` is sync (port contract). Call from a thread without a running
    event loop (``asyncio.run``); composition roots that already hold a loop
    should use an async adapter instead of this class.
    """

    def __init__(self, data_root: Path | str, *, catalog: Any) -> None:
        if catalog is None:
            raise TypeError("DataRootStudyService requires catalog= (no fallback)")
        self._root = Path(data_root)
        self._catalog = catalog

    def study_view(self, study_id: str) -> StudyView:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._study_view_async(study_id))
        raise RuntimeError(
            "DataRootStudyService.study_view requires no running event loop"
        )

    async def _study_view_async(self, study_id: str) -> StudyView:
        db_path = self._root / study_id / "events.db"
        if not db_path.is_file():
            raise StudyNotFound(study_id)
        from coscientist.api.runtime import StudyRuntime as ApiStudyRuntime
        from coscientist.core.execution.runtime_factory import close_runtime, open_runtime

        rt = await open_runtime(
            self._root, study_id, catalog=self._catalog, runtime_cls=ApiStudyRuntime
        )
        try:
            if rt.study is None:
                raise StudyNotFound(study_id)
            return rt.view()
        finally:
            await close_runtime(rt)
