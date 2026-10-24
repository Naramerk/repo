"""G4: Need + Plan.hypotheses/literature."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.core.events.upcasters import upcast
from coscientist.core.execution.domain import Need, TaskSpec
from coscientist.core.plan.need import (
    PlanNeedRefuse,
    need_from_plan_blob,
    resolve_plan_needs,
)
from tests.helpers import NEED_OFF, close_runtime, open_runtime


def test_need_reason_required() -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        Need(needed=True, reason="")
    with pytest.raises(ValidationError, match="Need.reason must be non-empty"):
        Need(needed=True, reason="  ")
    assert Need(needed=False, reason="no").needed is False


def test_need_from_plan_blob_corrupt() -> None:
    with pytest.raises(PlanNeedRefuse, match="plan_need_corrupt:missing_hypotheses"):
        need_from_plan_blob(None, "hypotheses")
    with pytest.raises(PlanNeedRefuse, match="plan_need_corrupt:missing_hypotheses"):
        need_from_plan_blob({}, "hypotheses")
    with pytest.raises(PlanNeedRefuse, match="plan_need_corrupt:missing_hypotheses"):
        need_from_plan_blob({"hypotheses": None}, "hypotheses")
    with pytest.raises(PlanNeedRefuse, match="plan_need_corrupt:missing_literature"):
        need_from_plan_blob(
            {"hypotheses": {"needed": False, "reason": "x"}}, "literature"
        )


def test_resolve_omit_refuses() -> None:
    with pytest.raises(PlanNeedRefuse, match="plan_need_required:hypotheses"):
        resolve_plan_needs(
            hypotheses=None,
            literature=Need(needed=False, reason="x"),
        )
    with pytest.raises(PlanNeedRefuse, match="plan_need_required:literature"):
        resolve_plan_needs(
            hypotheses=Need(needed=False, reason="x"),
            literature=None,
        )


def test_plan_accepted_upcaster_legacy_absent() -> None:
    v, out = upcast("PlanAccepted", 11, {"plan_id": "p", "tasks": []})
    assert v == 12
    assert out["hypotheses"] == {"needed": False, "reason": "legacy_absent"}
    assert out["literature"] == {"needed": False, "reason": "legacy_absent"}


@pytest.mark.asyncio
async def test_accept_plan_omit_refuses(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-omit")
    try:
        await rt.create_study(profile="t", request="q", catalog_version=3)
        pid = await rt.accept_plan(
            [TaskSpec(task_key="EXP-1", title="e", goal="e", route="experiment")]
        )
        assert pid == ""
        refused = [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
        assert refused and "plan_need_required" in str(refused[-1].payload)
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_explicit_need_off(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-need")
    try:
        await rt.create_study(profile="t", request="q", catalog_version=3)
        pid = await rt.accept_plan(
            [TaskSpec(task_key="EXP-1", title="e", goal="e", route="experiment")],
            hypotheses=NEED_OFF,
            literature=NEED_OFF,
        )
        assert pid
    finally:
        await close_runtime(rt)
