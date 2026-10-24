"""G4: Need + NeedUnrecorded + Plan.hypotheses/literature."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.core.events.events import PLAN_ACCEPTED_SCHEMA_VERSION
from coscientist.core.events.upcasters import upcast
from coscientist.core.execution.domain import Need, NeedUnrecorded, TaskSpec
from coscientist.core.plan.need import (
    PlanNeedRefuse,
    need_from_plan_blob,
    parse_plan_need,
    require_need,
    resolve_plan_needs,
    validate_need,
)
from tests.helpers import NEED_OFF, close_runtime, open_runtime

_UNRECORDED = {"source_schema_version": 11}


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"needed": True, "reason": ""}, "at least 1 character"),
        ({"needed": True, "reason": "  "}, r"Need\.reason must be non-empty"),
        ({"needed": "yes", "reason": "x"}, "valid boolean"),
        ({"needed": 1, "reason": "x"}, "valid boolean"),
    ],
)
def test_need_rejects_bad_shape(kwargs: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        Need.model_validate(kwargs)


def test_need_off_ok() -> None:
    assert Need(needed=False, reason="no").needed is False


@pytest.mark.parametrize(
    ("prev", "field", "match"),
    [
        (None, "hypotheses", "plan_need_corrupt:missing_hypotheses"),
        ({}, "hypotheses", "plan_need_corrupt:missing_hypotheses"),
        ({"hypotheses": None}, "hypotheses", "plan_need_corrupt:missing_hypotheses"),
        (
            {"hypotheses": {"needed": False, "reason": "x"}},
            "literature",
            "plan_need_corrupt:missing_literature",
        ),
    ],
)
def test_need_from_plan_blob_corrupt(prev, field, match) -> None:
    with pytest.raises(PlanNeedRefuse, match=match):
        need_from_plan_blob(prev, field)


@pytest.mark.parametrize(
    ("hypotheses", "literature", "match"),
    [
        (None, Need(needed=False, reason="x"), "plan_need_required:hypotheses"),
        (Need(needed=False, reason="x"), None, "plan_need_required:literature"),
    ],
)
def test_resolve_omit_refuses(hypotheses, literature, match) -> None:
    with pytest.raises(PlanNeedRefuse, match=match):
        resolve_plan_needs(hypotheses=hypotheses, literature=literature)


def test_validate_need_rejects_dict() -> None:
    with pytest.raises(PlanNeedRefuse, match="plan_need_type:expected_Need"):
        validate_need({"needed": False, "reason": "x"})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("payload", "hyp", "lit"),
    [
        ({"plan_id": "p", "tasks": []}, _UNRECORDED, _UNRECORDED),
        (
            {
                "plan_id": "p",
                "tasks": [],
                "hypotheses": {"needed": False, "reason": "x"},
            },
            {"needed": False, "reason": "x"},
            _UNRECORDED,
        ),
        (
            {
                "plan_id": "p",
                "tasks": [],
                "hypotheses": None,
                "literature": {"needed": True, "reason": "y"},
            },
            _UNRECORDED,
            {"needed": True, "reason": "y"},
        ),
    ],
)
def test_plan_accepted_v11_fills_need_unrecorded(payload, hyp, lit) -> None:
    """Real v11 omit/null → NeedUnrecorded (no invent, no upcast refuse)."""
    v, out = upcast("PlanAccepted", 11, payload)
    assert v == PLAN_ACCEPTED_SCHEMA_VERSION
    assert out["hypotheses"] == hyp
    assert out["literature"] == lit


def test_plan_accepted_v11_keeps_explicit_need() -> None:
    hyp = {"needed": False, "reason": "x"}
    lit = {"needed": True, "reason": "y"}
    v, out = upcast("PlanAccepted", 11, {"plan_id": "p", "tasks": [], "hypotheses": hyp, "literature": lit})
    assert v == PLAN_ACCEPTED_SCHEMA_VERSION
    assert out["hypotheses"] == hyp
    assert out["literature"] == lit


def test_require_need_refuses_unrecorded() -> None:
    with pytest.raises(PlanNeedRefuse, match=r"plan_need_unrecorded:source_schema_version=11"):
        require_need(NeedUnrecorded(source_schema_version=11))
    with pytest.raises(PlanNeedRefuse, match=r"plan_need_unrecorded:source_schema_version=11"):
        require_need(parse_plan_need(_UNRECORDED))
    assert require_need(Need(needed=False, reason="x")).needed is False


@pytest.mark.asyncio
async def test_accept_plan_omit_refuses(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-omit")
    try:
        await rt.create_study(profile="t", request="q", catalog_version=3)
        pid = await rt.accept_plan([TaskSpec(task_key="EXP-1", title="e", goal="e", route="experiment")])
        assert pid == ""
        refused = [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
        assert len(refused) == 1
        assert refused[0].payload["detail"].startswith("plan_need_required")
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
