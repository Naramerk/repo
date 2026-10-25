"""POST /studies/{id}/cancel (DESIGN §9). Opens runtime, cancels, closes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from coscientist.api.errors import CancelByRefuse
from coscientist.api.study_service import open_study_runtime
from coscientist.core.execution.domain import Catalog


def require_cancel_by(by: str) -> str:
    """Fail-closed non-empty ``by`` (no API default)."""
    if not by or by != by.strip():
        raise CancelByRefuse("by_empty_or_whitespace")
    return by


async def post_study_cancel(
    data_root: Path | str,
    study_id: str,
    *,
    catalog: Catalog,
    by: str,
) -> dict[str, str]:
    """Cancel a study. Missing journal → ``StudyNotFound``. ``catalog`` required."""
    by_s = require_cancel_by(by)
    async with open_study_runtime(data_root, study_id, catalog=catalog) as (sid, rt):
        await rt.cancel_study(by=by_s)
    return {"study_id": sid}


def bind_cancel_study(
    data_root: Path | str,
    *,
    catalog: Catalog,
) -> Callable[[str, str], Awaitable[dict[str, str]]]:
    """Composition-root adapter: ``AppDeps.cancel_study`` → ``post_study_cancel``."""

    async def cancel_study(study_id: str, by: str) -> dict[str, str]:
        return await post_study_cancel(data_root, study_id, catalog=catalog, by=by)

    return cancel_study
