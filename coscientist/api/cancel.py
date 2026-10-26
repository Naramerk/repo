"""POST /studies/{id}/cancel (DESIGN §9). Opens runtime, cancels, closes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from coscientist.api.console import read_events
from coscientist.api.errors import CancelByRefuse
from coscientist.api.runtime import StudyRuntime as ApiRuntime
from coscientist.api.study_service import open_study_runtime, study_events_db
from coscientist.core.events.errors import STUDY_NOT_FOUND_REASON, StudyNotFound
from coscientist.core.execution.domain import Catalog
from coscientist.experimental.runtime import StudyRuntime as InteractiveRuntime


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
    sid, db = study_events_db(Path(data_root), study_id)
    if not db.is_file():
        raise StudyNotFound(sid, reason=STUDY_NOT_FOUND_REASON)
    # The composition root interrupts any API owner before entering here. The
    # recorded policy selects capability; the core's default guard is unchanged.
    runtime_cls = ApiRuntime
    for event in read_events(Path(data_root), sid, 0):
        if event["type"] == "StudyCreated":
            payload = cast(dict[str, object], event["payload"])
            policy = payload["hitl_policy"]
            if isinstance(policy, dict) and policy.get("mode") == "interactive":
                runtime_cls = InteractiveRuntime
            break
    async with open_study_runtime(data_root, study_id, catalog=catalog, runtime_cls=runtime_cls) as (sid, rt):
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
