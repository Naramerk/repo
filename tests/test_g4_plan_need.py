"""G4: Need + Plan.hypotheses/literature + Study.require_hypotheses."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.upcasters import upcast, writer_schema_version
from coscientist.core.execution.domain import Need, Plan, TaskSpec
from coscientist.core.plan.lifecycle import LifecycleMixin
from coscientist.core.plan.need import (
    PlanNeedRefuse,
    assert_profile_hypotheses_satisfied,
    need_from_plan_blob,
    resolve_plan_needs,
    validate_need,
)
from tests.helpers import close_runtime, open_runtime


def test_need_validate_resolve_refuse_blob() -> None:
    n = validate_need({"needed": True, "reason": "test H0"})
    assert n.needed is True and validate_need(n).needed is True
    with pytest.raises(ValidationError, match="reason"):
        validate_need({"needed": False, "reason": ""})
    with pytest.raises(ValidationError, match="Need.reason must be non-empty"):
        validate_need({"needed": True, "reason": "   "})
    forced = Need(needed=True, reason="must")
    opt = Need(needed=False, reason="skip")
    assert_profile_hypotheses_satisfied(forced, require_hypotheses=True)
    with pytest.raises(
        PlanNeedRefuse, match="profile_requires_hypotheses:plan.hypotheses.needed_must_be_true"
    ):
        assert_profile_hypotheses_satisfied(opt, require_hypotheses=True)
    with pytest.raises(PlanNeedRefuse, match="plan_need_required:hypotheses"):
        resolve_plan_needs(hypotheses=None, literature=None, require_hypotheses=False)
    with pytest.raises(PlanNeedRefuse, match="plan_need_required:literature"):
        resolve_plan_needs(hypotheses=forced, literature=None, require_hypotheses=False)
    with pytest.raises(
        PlanNeedRefuse, match="profile_requires_hypotheses:plan.hypotheses.needed_must_be_true"
    ):
        resolve_plan_needs(
            hypotheses=opt,
            literature=Need(needed=False, reason="no lit"),
            require_hypotheses=True,
        )
    hyp, lit = resolve_plan_needs(
        hypotheses=Need(needed=True, reason="planner wants H"),
        literature=Need(needed=True, reason="need papers"),
        require_hypotheses=False,
    )
    assert hyp.reason == "planner wants H" and lit.needed is True
    with pytest.raises(PlanNeedRefuse, match="plan_need_corrupt"):
        need_from_plan_blob(None, "hypotheses")
    with pytest.raises(PlanNeedRefuse, match="plan_need_corrupt"):
        need_from_plan_blob({}, "literature")
    carried = need_from_plan_blob(
        {"hypotheses": {"needed": True, "reason": "carry"}}, "hypotheses"
    )
    assert carried.needed is True and carried.reason == "carry"


def test_upcasters_need_and_require_hypotheses() -> None:
    v, out = upcast(
        "PlanAccepted",
        11,
        {"plan_id": "pl_x", "study_id": "st_x", "revision": 1, "hypothesis_id": None, "tasks": []},
    )
    assert v == writer_schema_version("PlanAccepted")
    assert out["hypotheses"]["reason"] == "legacy_absent"
    assert out["literature"]["needed"] is False
    _, out_none = upcast(
        "PlanAccepted",
        11,
        {
            "plan_id": "pl_y",
            "study_id": "st_y",
            "revision": 1,
            "hypothesis_id": None,
            "tasks": [],
            "hypotheses": None,
            "literature": None,
        },
    )
    assert out_none["hypotheses"] is None and out_none["literature"] is None
    v2, out2 = upcast(
        "StudyCreated",
        13,
        {"study_id": "st_x", "profile": "research", "request": "q", "catalog_version": 1},
    )
    assert v2 == writer_schema_version("StudyCreated")
    assert out2["require_hypotheses"] is False


def test_plan_model_requires_need() -> None:
    with pytest.raises(ValidationError, match="hypotheses"):
        Plan(plan_id="pl", study_id="st", revision=1, hypothesis_id=None, tasks=[])


@pytest.mark.asyncio
async def test_accept_plan_persists_and_refuses(tmp_path) -> None:
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
        plans = [p for p in await rt.store.read_all() if p.type == "PlanAccepted"]
        assert len(plans) == 1
        assert plans[0].payload["hypotheses"] == {
            "needed": True,
            "reason": "test alternative H",
        }
        assert plans[0].schema_version == writer_schema_version("PlanAccepted")
        # Production path: omitted Need → PlanRefused (bypass test helper).
        rt.accept_plan = LifecycleMixin.accept_plan.__get__(rt, type(rt))
        pid = await rt.accept_plan(
            [TaskSpec(task_key="EXP-1", title="e", goal="e", route="experiment")],
        )
        assert pid == ""
        events = await rt.store.read_all()
        refused = [e for e in events if e.type == "PlanRefused"]
        assert refused and "plan_need_required" in str(refused[-1].payload)
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
            literature=Need(needed=False, reason="no lit"),
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
