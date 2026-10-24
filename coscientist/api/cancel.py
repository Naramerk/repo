"""POST /studies/{id}/cancel (DESIGN §9). Opens runtime, cancels, closes."""

from __future__ import annotations

from pathlib import Path

from coscientist.api.errors import CancelByRefuse
from coscientist.api.study_service import open_study_runtime
from coscientist.core.execution.domain import Catalog


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
    catalog: Catalog,
    by: object,
) -> dict[str, str]:
    """Cancel a study. Missing journal → ``StudyNotFound``. ``catalog`` required."""
    by_s = require_cancel_by(by)
    async with open_study_runtime(data_root, study_id, catalog=catalog) as (sid, rt):
        await rt.cancel_study(by=by_s)
    return {"study_id": sid}
