"""M5 API: POST /studies/{id}/cancel library contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from coscientist.api.cancel import (
    bind_cancel_study,
    post_study_cancel,
    require_cancel_by,
)
from coscientist.api.errors import CancelByRefuse
from coscientist.api.studies import CreateStudyBody, post_studies
from coscientist.core.events.errors import STUDY_NOT_FOUND_REASON, StudyNotFound
from coscientist.core.execution.runtime_factory import close_runtime
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import RC3T_THRESHOLDS, open_runtime


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        ("", "by_empty_or_whitespace"),
        ("  ", "by_empty_or_whitespace"),
        (" x", "by_empty_or_whitespace"),
    ],
    ids=["empty", "spaces", "padded"],
)
def test_require_cancel_by_refuses(bad: str, match: str) -> None:
    with pytest.raises(CancelByRefuse, match=match):
        require_cancel_by(bad)


@pytest.mark.asyncio
async def test_bind_cancel_study_closes(tmp_path: Path) -> None:
    """Composition path: ``bind_cancel_study`` → ``post_study_cancel`` → closed."""
    catalog = minimal_test_catalog()
    sid = (
        await post_studies(
            tmp_path,
            CreateStudyBody(
                request="cancel me",
                profile="research",
                catalog_version=3,
                require_hypotheses=False,
                **RC3T_THRESHOLDS,
            ),
            catalog=catalog,
        )
    )["study_id"]
    cancel = bind_cancel_study(tmp_path, catalog=catalog)
    assert await cancel(sid, "operator") == {"study_id": sid}
    rt = await open_runtime(tmp_path, sid, catalog=catalog)
    try:
        assert rt.view().phase == "closed"
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_post_study_cancel_missing(tmp_path: Path) -> None:
    missing = "st_" + "d" * 27
    with pytest.raises(StudyNotFound, match=STUDY_NOT_FOUND_REASON):
        await post_study_cancel(tmp_path, missing, catalog=minimal_test_catalog(), by="user")
