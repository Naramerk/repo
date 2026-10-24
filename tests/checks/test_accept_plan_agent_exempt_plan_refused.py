"""B1: agent ``exempt_from_freeze`` on a criterion → PlanRefused (not crash)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import NEED_OFF, close_runtime, open_runtime


@pytest.mark.asyncio
async def test_accept_plan_agent_exempt_from_freeze_is_plan_refused(tmp_path, clock):
    """Criterion dict with ``exempt_from_freeze=True`` → journaled PlanRefused.

    ``RequirementRef`` forbids the field (``extra=forbid``); accept_plan maps
    the ValidationError to PlanRefused instead of raising CatalogError.
    """
    cat = minimal_test_catalog()
    sid = "st_agent_exempt"
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=cat)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=cat.catalog_version,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")

    pid = await rt.accept_plan(
        [
            {
                "task_key": "E1",
                "title": "smuggle",
                "goal": "g",
                "route": "experiment",
                "depends_on": ["PLAN"],
                "criteria": [
                    {
                        "template_id": "artifact_present",
                        "exempt_from_freeze": True,
                    }
                ],
            }
        ]
    , hypotheses=NEED_OFF, literature=NEED_OFF)
    assert pid == ""
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "PlanRefused"]
    assert refused, "expected PlanRefused journaled (not CatalogError crash)"
    detail = str(refused[0].payload.get("detail") or "")
    assert "exempt_from_freeze" in detail
    assert any(e.type == "StudyFinalized" for e in events)
    await close_runtime(rt)
