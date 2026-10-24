"""G4: Need + NeedUnrecorded + Plan.hypotheses/literature."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.core.events.events import PLAN_ACCEPTED_NEED_SINCE, PLAN_ACCEPTED_SCHEMA_VERSION
from coscientist.core.events.upcasters import upcast
from coscientist.core.execution.domain import Need, NeedUnrecorded, TaskSpec
from coscientist.core.plan import expand, foreach, loop
from coscientist.core.plan.need import (
    PlanNeedRefuse,
    need_from_plan_blob,
    parse_plan_need,
    recorded_plan_needs,
    require_need,
    resolve_plan_needs,
)
from tests.helpers import NEED_OFF, NEED_UNRECORDED, close_runtime, open_runtime

_UNRECORDED_PREV: dict[str, Any] = {
    "hypotheses": dict(NEED_UNRECORDED),
    "literature": dict(NEED_UNRECORDED),
}
_NEED_OFF_DUMP = {"kind": "need", "needed": False, "reason": "x"}
_NEED_ON_DUMP = {"kind": "need", "needed": True, "reason": "y"}
_UNRECORDED_MATCH = rf"plan_need_unrecorded:source_schema_version={PLAN_ACCEPTED_NEED_SINCE}"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"needed": True, "reason": ""}, "at least 1 character"),
        ({"needed": True, "reason": "  "}, r"Need\.reason must be non-empty"),
        ({"needed": "yes", "reason": "x"}, "valid boolean"),
        ({"needed": 1, "reason": "x"}, "valid boolean"),
    ],
)
def test_need_rejects_bad_shape(kwargs, match) -> None:
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
            {"hypotheses": {"kind": "need", "needed": False, "reason": "x"}},
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


@pytest.mark.parametrize(
    ("payload", "hyp", "lit"),
    [
        ({"plan_id": "p", "tasks": []}, NEED_UNRECORDED, NEED_UNRECORDED),
        (
            {
                "plan_id": "p",
                "tasks": [],
                "hypotheses": {"needed": False, "reason": "x"},
            },
            _NEED_OFF_DUMP,
            NEED_UNRECORDED,
        ),
        (
            {
                "plan_id": "p",
                "tasks": [],
                "hypotheses": None,
                "literature": {"needed": True, "reason": "y"},
            },
            NEED_UNRECORDED,
            _NEED_ON_DUMP,
        ),
    ],
    ids=["both_absent", "lit_absent", "hyp_null"],
)
def test_plan_accepted_v11_fills_need_unrecorded(payload, hyp, lit) -> None:
    """Real v11 omit/null → NeedUnrecorded (no invent, no upcast refuse)."""
    v, out = upcast("PlanAccepted", PLAN_ACCEPTED_NEED_SINCE, payload)
    assert v == PLAN_ACCEPTED_SCHEMA_VERSION
    assert out["hypotheses"] == hyp
    assert out["literature"] == lit


def test_plan_accepted_v11_keeps_explicit_need() -> None:
    hyp = {"needed": False, "reason": "x"}
    lit = {"needed": True, "reason": "y"}
    v, out = upcast(
        "PlanAccepted",
        PLAN_ACCEPTED_NEED_SINCE,
        {"plan_id": "p", "tasks": [], "hypotheses": hyp, "literature": lit},
    )
    assert v == PLAN_ACCEPTED_SCHEMA_VERSION
    assert out["hypotheses"] == _NEED_OFF_DUMP
    assert out["literature"] == _NEED_ON_DUMP


def test_require_need_refuses_need_unrecorded_model() -> None:
    with pytest.raises(PlanNeedRefuse, match=_UNRECORDED_MATCH):
        require_need(NeedUnrecorded(source_schema_version=PLAN_ACCEPTED_NEED_SINCE))


def test_require_need_refuses_parsed_unrecorded() -> None:
    with pytest.raises(PlanNeedRefuse, match=_UNRECORDED_MATCH):
        require_need(parse_plan_need(NEED_UNRECORDED))


def test_require_need_accepts_need() -> None:
    assert require_need(Need(needed=False, reason="x")).needed is False


@pytest.mark.parametrize(
    "mod",
    [expand, foreach, loop],
    ids=["expand", "foreach", "loop"],
)
def test_prod_plan_consumer_refuses_need_unrecorded(mod) -> None:
    """expand / foreach / loop bind recorded_plan_needs → typed refuse at use."""
    assert mod.recorded_plan_needs is recorded_plan_needs
    with pytest.raises(PlanNeedRefuse, match=_UNRECORDED_MATCH):
        mod.recorded_plan_needs(_UNRECORDED_PREV)


@pytest.mark.asyncio
async def test_accept_plan_omit_refuses(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-omit")
    try:
        await rt.create_study(profile="t", request="q", catalog_version=3)
        pid = await rt.accept_plan([TaskSpec(task_key="EXP-1", title="e", goal="e", route="experiment")])
        assert pid == ""
        refused = [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
        assert len(refused) == 1
        assert refused[0].payload["detail"] == "plan_need_required:hypotheses"
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
