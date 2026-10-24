"""G4: Need + NeedUnrecorded + Plan.hypotheses/literature."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import PLAN_ACCEPTED_NEED_SINCE, PLAN_ACCEPTED_SCHEMA_VERSION
from coscientist.core.events.ids import task_id
from coscientist.core.events.upcasters import upcast
from coscientist.core.execution.domain import (
    ForEachSpec,
    LoopPredicate,
    LoopSpec,
    Need,
    NeedUnrecorded,
    QuestionSpec,
    TaskSpec,
)
from coscientist.core.plan.foreach import materialize_foreach
from coscientist.core.plan.loop import materialize_loop_iter
from coscientist.core.plan.need import (
    NeedKind,
    PlanNeedRefuse,
    need_from_plan_blob,
    parse_plan_need,
    require_need,
    resolve_plan_needs,
)
from tests.helpers import NEED_OFF, NEED_UNRECORDED, close_runtime, open_runtime

_OFF = {"kind": "need", "needed": False, "reason": "x"}
_DETAIL = f"plan_need_unrecorded:source_schema_version={PLAN_ACCEPTED_NEED_SINCE}"
_MATCH = rf"plan_need_unrecorded:source_schema_version={PLAN_ACCEPTED_NEED_SINCE}"
_EXP = TaskSpec(task_key="EXP-1", title="e", goal="e", route="experiment")
_E0 = TaskSpec(task_key="E0", title="e", goal="e", route="experiment")
_FE = TaskSpec(
    task_key="FE",
    title="fe",
    goal="fe",
    route="experiment",
    depends_on=["E0"],
    foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
)
_LOOP = LoopSpec(
    loop_id="L1",
    body=[TaskSpec(task_key="BODY", title="b", goal="b", route="experiment", addresses=["Q1"])],
    until=LoopPredicate(name="survivors_le", params={"n": 0}),
    max_iter=2,
    scope_questions=["Q1"],
)
_Q1 = [QuestionSpec(id="Q1", text="q")]


def _unrec(rt: Any) -> None:
    for b in rt.plans.values():
        b["hypotheses"] = dict(NEED_UNRECORDED)
        b["literature"] = dict(NEED_UNRECORDED)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"needed": True, "reason": ""}, "at least 1 character"),
        ({"needed": True, "reason": "  "}, r"Need\.reason must be non-empty"),
        ({"needed": "yes", "reason": "x"}, "valid boolean"),
        ({"needed": 1, "reason": "x"}, "valid boolean"),
    ],
    ids=["empty_reason", "whitespace_reason", "str_bool", "int_bool"],
)
def test_need_rejects_bad_shape(kwargs: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        Need.model_validate(kwargs)


@pytest.mark.parametrize(
    ("prev", "field", "match"),
    [
        ({}, "hypotheses", "plan_need_corrupt:missing_hypotheses"),
        ({"hypotheses": None}, "hypotheses", "plan_need_corrupt:missing_hypotheses"),
        ({"hypotheses": _OFF}, "literature", "plan_need_corrupt:missing_literature"),
        ({"hypotheses": "x"}, "hypotheses", "plan_need_corrupt:bad_shape_hypotheses"),
        (
            {"hypotheses": {"kind": "need", "needed": False}},
            "hypotheses",
            "plan_need_corrupt:bad_shape_hypotheses",
        ),
    ],
    ids=["key_absent", "value_null", "lit_missing", "not_mapping", "du_invalid"],
)
def test_need_from_plan_blob_corrupt(
    prev: dict[str, Any] | None, field: NeedKind, match: str
) -> None:
    with pytest.raises(PlanNeedRefuse, match=match):
        need_from_plan_blob(prev, field)


@pytest.mark.parametrize(
    ("hypotheses", "literature", "match"),
    [
        (None, Need(needed=False, reason="x"), "plan_need_required:hypotheses"),
        (Need(needed=False, reason="x"), None, "plan_need_required:literature"),
    ],
    ids=["hyp_omit", "lit_omit"],
)
def test_resolve_omit_refuses(
    hypotheses: Need | None, literature: Need | None, match: str
) -> None:
    with pytest.raises(PlanNeedRefuse, match=match):
        resolve_plan_needs(hypotheses=hypotheses, literature=literature)


@pytest.mark.parametrize(
    ("payload", "hyp", "lit"),
    [
        ({"plan_id": "p", "tasks": []}, NEED_UNRECORDED, NEED_UNRECORDED),
        ({"plan_id": "p", "tasks": [], "hypotheses": dict(_OFF)}, _OFF, NEED_UNRECORDED),
    ],
    ids=["both_absent", "lit_absent"],
)
def test_plan_accepted_v11_fills_need_unrecorded(
    payload: dict, hyp: dict, lit: dict
) -> None:
    """v11 absent → NeedUnrecorded; present with kind → strict DU dump."""
    v, out = upcast("PlanAccepted", PLAN_ACCEPTED_NEED_SINCE, payload)
    assert v == PLAN_ACCEPTED_SCHEMA_VERSION
    assert out["hypotheses"] == hyp
    assert out["literature"] == lit


@pytest.mark.parametrize(
    ("blob", "match"),
    [
        (None, "need_not_mapping:hypotheses"),
        ({"needed": False, "reason": "x"}, "need_kind_missing:hypotheses"),
        ({"kind": None, "needed": False, "reason": "x"}, "need_kind_null:hypotheses"),
        ({"kind": "weird", "needed": False, "reason": "x"}, "need_kind_unknown:hypotheses"),
        ({"kind": "need", "needed": False}, "need_invalid:hypotheses"),
    ],
    ids=["null", "kind_missing", "kind_null", "kind_unknown", "du_invalid"],
)
def test_plan_accepted_v11_need_corrupt(blob: Any, match: str) -> None:
    payload = {
        "plan_id": "p",
        "tasks": [],
        "hypotheses": blob,
        "literature": dict(NEED_UNRECORDED),
    }
    with pytest.raises(JournalFormatError, match=match):
        upcast("PlanAccepted", PLAN_ACCEPTED_NEED_SINCE, payload)


def test_require_need_refuses_need_unrecorded_model() -> None:
    with pytest.raises(PlanNeedRefuse, match=_MATCH):
        require_need(NeedUnrecorded(source_schema_version=PLAN_ACCEPTED_NEED_SINCE))


def test_require_need_refuses_parsed_unrecorded() -> None:
    with pytest.raises(PlanNeedRefuse, match=_MATCH):
        require_need(parse_plan_need(NEED_UNRECORDED))


async def _assert_one_refuse(rt: Any, etype: str) -> None:
    refused = [e for e in await rt.store.read_all() if e.type == etype]
    assert len(refused) == 1
    assert refused[0].payload["detail"] == _DETAIL


@pytest.mark.asyncio
async def test_expand_refuses_need_unrecorded(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-exp-unrec")
    try:
        await rt.create_study(profile="t", request="q", catalog_version=3)
        await rt.accept_plan([_EXP], hypotheses=NEED_OFF, literature=NEED_OFF)
        _unrec(rt)
        pid = await rt.expand_plan(
            after_task="EXP-1",
            added_tasks=[TaskSpec(task_key="EXP-2", title="e2", goal="e2", route="experiment")],
        )
        assert pid is None
        await _assert_one_refuse(rt, "ExpansionRefused")
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_refuses_need_unrecorded(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-fe-unrec")
    try:
        await rt.create_study(profile="t", request="q", catalog_version=3)
        await rt.accept_plan(
            [_E0, _FE], questions=_Q1, hypotheses=NEED_OFF, literature=NEED_OFF
        )
        _unrec(rt)
        pid = await materialize_foreach(
            rt, template=rt.tasks[task_id("g4-fe-unrec", "FE")], items=["H1"]
        )
        assert pid is None
        await _assert_one_refuse(rt, "ExpansionRefused")
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_refuses_need_unrecorded(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-loop-unrec")
    try:
        await rt.create_study(profile="t", request="q", catalog_version=3)
        await rt.accept_plan(
            [], questions=_Q1, loops=[], hypotheses=NEED_OFF, literature=NEED_OFF
        )
        _unrec(rt)
        assert await materialize_loop_iter(rt, _LOOP, iter_n=1) is None
        await _assert_one_refuse(rt, "LoopRefused")
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_omit_refuses(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-omit")
    try:
        await rt.create_study(profile="t", request="q", catalog_version=3)
        assert await rt.accept_plan([_EXP]) == ""
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
        assert await rt.accept_plan([_EXP], hypotheses=NEED_OFF, literature=NEED_OFF)
    finally:
        await close_runtime(rt)
