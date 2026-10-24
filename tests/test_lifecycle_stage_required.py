"""Intake stage ``TaskSpec.required`` — HYP/LIT optional (DESIGN §2 Need), REPORT optional."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.execution.domain import TaskKind
from tests.helpers import close_runtime, open_runtime


@pytest.mark.asyncio
async def test_intake_hyp_lit_report_not_required(tmp_path, clock) -> None:
    """HYP/LIT/REPORT required=False so Need-off cancel/SKIPPED can still fulfill."""
    sid = "st_stage_required"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    try:
        await rt.create_study(
            profile="research",
            request="stage required flags",
            catalog_version=1,
            deadline=clock.now() + timedelta(seconds=600),
            sid=sid,
            finalize_reserve_s=30,
            require_hypotheses=False,
        )
        by_key = {t.spec.task_key: t for t in rt.tasks.values()}
        assert by_key["SPEC"].spec.required is True
        assert by_key["SPEC"].kind == TaskKind.SPEC
        assert [o.name for o in by_key["SPEC"].spec.expected_outputs] == ["main"]
        assert by_key["HYP"].spec.required is False
        assert by_key["HYP"].kind == TaskKind.HYPOTHESES
        assert by_key["LIT"].spec.required is False
        assert by_key["LIT"].kind == TaskKind.LITERATURE
        assert by_key["PLAN"].spec.required is True
        assert by_key["PLAN"].spec.expected_outputs == []
        assert by_key["REPORT"].spec.required is False
        assert by_key["REPORT"].kind == TaskKind.REPORT
    finally:
        await close_runtime(rt)
