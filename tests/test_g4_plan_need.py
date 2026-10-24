"""G4: Need + Plan.hypotheses/literature + Study.require_hypotheses."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.upcasters import upcast, writer_schema_version
from coscientist.core.execution.domain import Need, Plan, TaskSpec
from coscientist.core.plan.need import (
    LEGACY_ABSENT_REASON,
    PlanNeedRefuse,
    assert_profile_hypotheses_satisfied,
    legacy_absent_need,
    need_from_plan_blob,
    profile_default_need,
    resolve_plan_needs,
    validate_need,
)
from tests.helpers import close_runtime, open_runtime


def test_need_validate_legacy_profile_resolve_blob() -> None:
    n = validate_need({"needed": True, "reason": "test H0"})
    assert n.needed is True and validate_need(n).needed is True
    with pytest.raises((ValidationError, ValueError)):
        validate_need({"needed": False, "reason": ""})
    with pytest.raises((ValidationError, ValueError)):
        validate_need({"needed": True, "reason": "   "})
    assert legacy_absent_need().reason == LEGACY_ABSENT_REASON
    forced = profile_default_need(kind="hypotheses", require_hypotheses=True)
    assert forced.needed is True and forced.reason == "profile_requires_hypotheses"
    opt = profile_default_need(kind="hypotheses", require_hypotheses=False)
    assert opt.needed is False
    lit = profile_default_need(kind="literature", require_hypotheses=True)
    assert lit.needed is False and lit.reason == "profile_optional_literature"
    assert_profile_hypotheses_satisfied(forced, require_hypotheses=True)
    with pytest.raises(PlanNeedRefuse):
        assert_profile_hypotheses_satisfied(opt, require_hypotheses=True)
    hyp, lit2 = resolve_plan_needs(
        hypotheses=None, literature=None, require_hypotheses=True
    )
    assert hyp.needed is True and lit2.needed is False
    with pytest.raises(PlanNeedRefuse):
        resolve_plan_needs(
            hypotheses=Need(needed=False, reason="no"),
            literature=None,
            require_hypotheses=True,
        )
    hyp2, lit3 = resolve_plan_needs(
        hypotheses=Need(needed=True, reason="planner wants H"),
        literature=Need(needed=True, reason="need papers"),
        require_hypotheses=False,
    )
    assert hyp2.reason == "planner wants H" and lit3.needed is True
    assert need_from_plan_blob(None, "hypotheses").reason == LEGACY_ABSENT_REASON
    assert need_from_plan_blob({}, "literature").reason == LEGACY_ABSENT_REASON
    carried = need_from_plan_blob(
        {"hypotheses": {"needed": True, "reason": "carry"}}, "hypotheses"
    )
    assert carried.needed is True and carried.reason == "carry"


def test_upcasters_need_and_require_hypotheses() -> None:
    v, out = upcast(
        "PlanAccepted",
        11,
        {
            "plan_id": "pl_x",
            "study_id": "st_x",
            "revision": 1,
            "hypothesis_id": None,
            "tasks": [],
        },
    )
    assert v == writer_schema_version("PlanAccepted")
    assert out["hypotheses"]["reason"] == LEGACY_ABSENT_REASON
    assert out["literature"]["needed"] is False
    v2, out2 = upcast(
        "StudyCreated",
        13,
        {
            "study_id": "st_x",
            "profile": "research",
            "request": "q",
            "catalog_version": 1,
        },
    )
    assert v2 == writer_schema_version("StudyCreated")
    assert out2["require_hypotheses"] is False


def test_plan_model_requires_need() -> None:
    with pytest.raises(ValidationError):
        Plan(
            plan_id="pl",
            study_id="st",
            revision=1,
            hypothesis_id=None,
            tasks=[],
        )


@pytest.mark.asyncio
async def test_accept_plan_persists_need(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    rt = await open_runtime(tmp_path, "st_g4_need", clock=clock)
    try:
        await rt.create_study(
            profile="research",
            request="does x?",
            catalog_version=1,
            deadline=clock.now() + timedelta(hours=2),
        )
        await rt.accept_plan(
            [TaskSpec(task_key="EXP-0", title="e", goal="e", route="experiment")],
            hypotheses=Need(needed=True, reason="test alternative H"),
            literature=Need(needed=False, reason="no lit this round"),
        )
        plans = [p for p in (await rt.store.read_all()) if p.type == "PlanAccepted"]
        assert len(plans) == 1
        assert plans[0].payload["hypotheses"] == {
            "needed": True,
            "reason": "test alternative H",
        }
        assert plans[0].payload["literature"]["needed"] is False
        assert plans[0].schema_version == writer_schema_version("PlanAccepted")
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_profile_requires_hypotheses_refuses(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    rt = await open_runtime(tmp_path, "st_g4_req", clock=clock)
    try:
        await rt.create_study(
            profile="research",
            request="does x?",
            catalog_version=1,
            deadline=clock.now() + timedelta(hours=2),
            require_hypotheses=True,
        )
        assert rt.study is not None and rt.study.require_hypotheses is True
        await rt.accept_plan(
            [TaskSpec(task_key="EXP-0", title="e", goal="e", route="experiment")],
            hypotheses=Need(needed=False, reason="skip hyps"),
        )
        events = await rt.store.read_all()
        assert not any(e.type == "PlanAccepted" for e in events)
        assert any(e.type == "PlanRefused" for e in events)
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_create_study_persists_require_hypotheses(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    rt = await open_runtime(tmp_path, "st_g4_flag", clock=clock)
    try:
        await rt.create_study(
            profile="research",
            request="q",
            catalog_version=1,
            deadline=clock.now() + timedelta(hours=2),
            require_hypotheses=True,
        )
        created = [e for e in await rt.store.read_all() if e.type == "StudyCreated"]
        assert created[0].payload["require_hypotheses"] is True
        assert created[0].schema_version == writer_schema_version("StudyCreated")
    finally:
        await close_runtime(rt)
