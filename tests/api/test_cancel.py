"""M5 API: POST /studies/{id}/cancel library contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from coscientist.api.cancel import post_study_cancel, require_cancel_by
from coscientist.api.errors import CancelByRefuse, StudyNotFound
from coscientist.api.studies import CreateStudyBody, post_studies
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import open_runtime


@pytest.mark.parametrize("bad", [None, 1, True, "", "  ", " x"])
def test_require_cancel_by_refuses(bad: object) -> None:
    with pytest.raises(CancelByRefuse):
        require_cancel_by(bad)


@pytest.mark.asyncio
async def test_post_study_cancel_closes_study(tmp_path: Path) -> None:
    catalog = minimal_test_catalog()
    sid = (
        await post_studies(
            tmp_path,
            CreateStudyBody(
                request="cancel me",
                profile="research",
                catalog_version=3,
                require_hypotheses=False,
            ),
            catalog=catalog,
        )
    )["study_id"]
    out = await post_study_cancel(tmp_path, sid, catalog=catalog, by="operator")
    assert out == {"study_id": sid}
    rt = await open_runtime(tmp_path, sid, catalog=catalog)
    try:
        assert rt.view().phase == "closed"
    finally:
        from coscientist.core.execution.runtime_factory import close_runtime

        await close_runtime(rt)


@pytest.mark.asyncio
async def test_post_study_cancel_missing_and_catalog(tmp_path: Path) -> None:
    with pytest.raises(StudyNotFound):
        await post_study_cancel(
            tmp_path, "st_missing", catalog=minimal_test_catalog(), by="user"
        )
    with pytest.raises(TypeError, match="catalog"):
        await post_study_cancel(tmp_path, "st_x", catalog=None, by="user")
