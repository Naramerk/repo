"""POST /studies/{id}/cancel (DESIGN §9). Opens runtime, cancels, closes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coscientist.api.errors import CancelByRefuse
from coscientist.api.study_service import study_events_db
from coscientist.api.views import require_study_id
from coscientist.core.events.errors import StudyNotFound


def require_cancel_by(by: object) -> str:
    """Fail-closed ``by`` (no API default)."""
    if not isinstance(by, str):
        raise CancelByRefuse(f"by must be str, got {type(by).__name__}")
    if not by or by != by.strip():
        raise CancelByRefuse("by_empty_or_whitespace")
    return by


async def post_study_cancel(
    data_root: Path | str,
    study_id: object,
    *,
    catalog: Any,
    by: object,
) -> dict[str, str]:
    """Cancel a study. Missing journal → ``StudyNotFound``. ``catalog`` required."""
    if catalog is None:
        raise TypeError("post_study_cancel requires catalog= (no test fallback)")
    sid = require_study_id(study_id)
    by_s = require_cancel_by(by)
    root = Path(data_root)
    if not study_events_db(root, sid).is_file():
        raise StudyNotFound(sid, reason="study_not_found")

    from coscientist.api.runtime import StudyRuntime as ApiStudyRuntime
    from coscientist.core.execution.runtime_factory import close_runtime, open_runtime

    rt = await open_runtime(root, sid, catalog=catalog, runtime_cls=ApiStudyRuntime)
    try:
        if rt.study is None:
            raise StudyNotFound(sid, reason="study_not_found")
        await rt.cancel_study(by=by_s)
    finally:
        await close_runtime(rt)
    return {"study_id": sid}
