"""G4: Need + NeedUnrecorded + Plan Needs + Study.require_hypotheses."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.core.events.errors import CatalogError, JournalFormatError
from coscientist.core.events.events import (
    PLAN_ACCEPTED_NEED_SINCE,
    PLAN_ACCEPTED_SCHEMA_VERSION,
    STUDY_CREATED_REQUIRE_HYPOTHESES_SINCE,
    STUDY_CREATED_SCHEMA_VERSION,
)
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
from coscientist.core.execution.machine import StudyRuntime
from coscientist.core.plan.foreach import materialize_foreach
from coscientist.core.plan.loop import materialize_loop_iter
from coscientist.core.plan.need import (
    NeedKind,
    PlanNeedRefuse,
    assert_profile_hypotheses_satisfied,
    need_from_plan_blob,
    parse_plan_need,
    require_need,
    resolve_plan_needs,
)
from tests.helpers import (
    NEED_OFF,
    NEED_UNRECORDED,
    active_plan_blob_for_tests,
    append_plan_accepted,
    close_runtime,
    open_runtime,
    plan_revision,
)

_OFF = {"kind": "need", "needed": False, "reason": "x"}
_UNRECORDED_DETAIL = f"plan_need_unrecorded:source_schema_version={PLAN_ACCEPTED_NEED_SINCE}"
_CORRUPT_DETAIL = "plan_need_corrupt:bad_shape_hypotheses"
_PROFILE_DETAIL = "profile_requires_hypotheses:plan.hypotheses.needed_must_be_true"
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
_NEED_X = Need(kind="need", needed=False, reason="x")
_NEED_ON = Need(kind="need", needed=True, reason="profile")
_UNREC = NeedUnrecorded(
    kind="unrecorded", source_schema_version=PLAN_ACCEPTED_NEED_SINCE
)


async def _supersede_via_v11_upcast(rt: StudyRuntime) -> None:
    """Strip Need → upcast from PLAN_ACCEPTED_NEED_SINCE → append_plan_accepted."""
    prev = active_plan_blob_for_tests(rt)
    rev = plan_revision(prev)
    v11 = {k: v for k, v in prev.items() if k not in ("hypotheses", "literature")}
    v11["revision"] = rev + 1
    v11["plan_id"] = f"{prev['plan_id']}-unrec"
    _, payload = upcast("PlanAccepted", PLAN_ACCEPTED_NEED_SINCE, v11)
    await append_plan_accepted(rt, payload, schema_version=PLAN_ACCEPTED_SCHEMA_VERSION)


async def _supersede_with_corrupt_hypotheses(rt: StudyRuntime) -> None:
    """Append writer-version PlanAccepted with non-Need hypotheses (corrupt).

    Fail-closed payload schemas reject ``hypotheses: str`` on the journal wire.
    """
    prev = active_plan_blob_for_tests(rt)
    rev = plan_revision(prev)
    payload = dict(prev)
    payload["revision"] = rev + 1
    payload["plan_id"] = f"{prev['plan_id']}-corrupt"
    payload["hypotheses"] = "not-a-need"
    await append_plan_accepted(rt, payload, schema_version=PLAN_ACCEPTED_SCHEMA_VERSION)


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
        Need.model_validate({"kind": "need", **kwargs})


def test_need_rejects_missing_kind() -> None:
    with pytest.raises(ValidationError, match="kind"):
        Need.model_validate({"needed": False, "reason": "x"})


@pytest.mark.parametrize(
    ("prev", "field", "match"),
    [
        (None, "hypotheses", "plan_need_corrupt:missing_hypotheses"),
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
    ids=["prev_none", "key_absent", "value_null", "lit_missing", "not_mapping", "du_invalid"],
)
def test_need_from_plan_blob_corrupt(
    prev: dict[str, Any] | None, field: NeedKind, match: str
) -> None:
    with pytest.raises(PlanNeedRefuse, match=match) as ei:
        need_from_plan_blob(prev, field)
    assert ei.value.reason == "invalid"


@pytest.mark.parametrize(
    ("hypotheses", "literature", "match"),
    [
        (None, _NEED_X, "plan_need_required:hypotheses"),
        (_NEED_X, None, "plan_need_required:literature"),
    ],
    ids=["hyp_omit", "lit_omit"],
)
def test_resolve_omit_refuses(
    hypotheses: Need | None, literature: Need | None, match: str
) -> None:
    with pytest.raises(PlanNeedRefuse, match=match) as ei:
        resolve_plan_needs(
            hypotheses=hypotheses,
            literature=literature,
            require_hypotheses=False,
        )
    assert ei.value.reason == "plan_need_required"


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
    with pytest.raises(PlanNeedRefuse, match=_UNRECORDED_DETAIL) as ei:
        require_need(_UNREC)
    assert ei.value.reason == "plan_need_unrecorded"


def test_require_need_refuses_parsed_unrecorded() -> None:
    with pytest.raises(PlanNeedRefuse, match=_UNRECORDED_DETAIL) as ei:
        require_need(parse_plan_need(NEED_UNRECORDED))
    assert ei.value.reason == "plan_need_unrecorded"


def test_require_need_accepts_need() -> None:
    assert require_need(_NEED_X).needed is False


async def _assert_one_refuse(
    rt: StudyRuntime, etype: str, *, reason: str, detail: str
) -> None:
    refused = [e for e in await rt.store.read_all() if e.type == etype]
    assert len(refused) == 1
    assert refused[0].payload["detail"] == detail
    assert refused[0].payload["reason"] == reason


@pytest.mark.asyncio
async def test_expand_refuses_need_unrecorded(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-exp-unrec")
    try:
        await rt.create_study(
            profile="t", request="q", catalog_version=3, require_hypotheses=False
        )
        await rt.accept_plan([_EXP], hypotheses=NEED_OFF, literature=NEED_OFF)
        await _supersede_via_v11_upcast(rt)
        pid = await rt.expand_plan(
            after_task="EXP-1",
            added_tasks=[TaskSpec(task_key="EXP-2", title="e2", goal="e2", route="experiment")],
        )
        assert pid is None
        await _assert_one_refuse(
            rt,
            "ExpansionRefused",
            reason="plan_need_unrecorded",
            detail=_UNRECORDED_DETAIL,
        )
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_refuses_need_unrecorded(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-fe-unrec")
    try:
        await rt.create_study(
            profile="t", request="q", catalog_version=3, require_hypotheses=False
        )
        await rt.accept_plan(
            [_E0, _FE], questions=_Q1, hypotheses=NEED_OFF, literature=NEED_OFF
        )
        await _supersede_via_v11_upcast(rt)
        pid = await materialize_foreach(
            rt, template=rt.tasks[task_id("g4-fe-unrec", "FE")], items=["H1"]
        )
        assert pid is None
        await _assert_one_refuse(
            rt,
            "ExpansionRefused",
            reason="plan_need_unrecorded",
            detail=_UNRECORDED_DETAIL,
        )
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_refuses_need_unrecorded(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-loop-unrec")
    try:
        await rt.create_study(
            profile="t", request="q", catalog_version=3, require_hypotheses=False
        )
        await rt.accept_plan(
            [], questions=_Q1, loops=[], hypotheses=NEED_OFF, literature=NEED_OFF
        )
        await _supersede_via_v11_upcast(rt)
        assert await materialize_loop_iter(rt, _LOOP, iter_n=1) is None
        await _assert_one_refuse(
            rt,
            "LoopRefused",
            reason="plan_need_unrecorded",
            detail=_UNRECORDED_DETAIL,
        )
        await _assert_one_refuse(
            rt,
            "ExpansionRefused",
            reason="plan_need_unrecorded",
            detail=_UNRECORDED_DETAIL,
        )
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_plan_accepted_wire_refuses_corrupt_hypotheses_str(tmp_path) -> None:
    """Corrupt ``hypotheses: str`` appends, then replay/read refuses at schema gate."""
    rt = await open_runtime(tmp_path, "g4-wire-bad")
    try:
        await rt.create_study(
            profile="t", request="q", catalog_version=3, require_hypotheses=False
        )
        await rt.accept_plan([_EXP], hypotheses=NEED_OFF, literature=NEED_OFF)
        await _supersede_with_corrupt_hypotheses(rt)
        with pytest.raises(JournalFormatError, match="payload schema refuse"):
            await rt.store.read_all()
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_expand_refuses_need_invalid(tmp_path) -> None:
    """Projection-level corrupt Need still → ExpansionRefused (defense in depth)."""
    rt = await open_runtime(tmp_path, "g4-exp-bad")
    try:
        await rt.create_study(
            profile="t", request="q", catalog_version=3, require_hypotheses=False
        )
        await rt.accept_plan([_EXP], hypotheses=NEED_OFF, literature=NEED_OFF)
        prev = active_plan_blob_for_tests(rt)
        bad = dict(prev)
        bad["revision"] = plan_revision(prev) + 1
        bad["plan_id"] = f"{prev['plan_id']}-corrupt"
        bad["hypotheses"] = "not-a-need"
        rt.plans[bad["plan_id"]] = bad
        pid = await rt.expand_plan(
            after_task="EXP-1",
            added_tasks=[TaskSpec(task_key="EXP-2", title="e2", goal="e2", route="experiment")],
        )
        assert pid is None
        await _assert_one_refuse(
            rt, "ExpansionRefused", reason="invalid", detail=_CORRUPT_DETAIL
        )
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_refuses_need_invalid(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-fe-bad")
    try:
        await rt.create_study(
            profile="t", request="q", catalog_version=3, require_hypotheses=False
        )
        await rt.accept_plan(
            [_E0, _FE], questions=_Q1, hypotheses=NEED_OFF, literature=NEED_OFF
        )
        prev = active_plan_blob_for_tests(rt)
        bad = dict(prev)
        bad["revision"] = plan_revision(prev) + 1
        bad["plan_id"] = f"{prev['plan_id']}-corrupt"
        bad["hypotheses"] = "not-a-need"
        rt.plans[bad["plan_id"]] = bad
        pid = await materialize_foreach(
            rt, template=rt.tasks[task_id("g4-fe-bad", "FE")], items=["H1"]
        )
        assert pid is None
        await _assert_one_refuse(
            rt, "ExpansionRefused", reason="invalid", detail=_CORRUPT_DETAIL
        )
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_refuses_need_invalid(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-loop-bad")
    try:
        await rt.create_study(
            profile="t", request="q", catalog_version=3, require_hypotheses=False
        )
        await rt.accept_plan(
            [], questions=_Q1, loops=[], hypotheses=NEED_OFF, literature=NEED_OFF
        )
        prev = active_plan_blob_for_tests(rt)
        bad = dict(prev)
        bad["revision"] = plan_revision(prev) + 1
        bad["plan_id"] = f"{prev['plan_id']}-corrupt"
        bad["hypotheses"] = "not-a-need"
        rt.plans[bad["plan_id"]] = bad
        assert await materialize_loop_iter(rt, _LOOP, iter_n=1) is None
        await _assert_one_refuse(
            rt, "LoopRefused", reason="invalid", detail=_CORRUPT_DETAIL
        )
        await _assert_one_refuse(
            rt, "ExpansionRefused", reason="invalid", detail=_CORRUPT_DETAIL
        )
    finally:
        await close_runtime(rt)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"study_id": "s", "catalog_version": 3}, "require_hypotheses_required"),
        (
            {"study_id": "s", "catalog_version": 3, "require_hypotheses": None},
            "require_hypotheses_required",
        ),
        (
            {"study_id": "s", "catalog_version": 3, "require_hypotheses": "yes"},
            "require_hypotheses_must_be_bool",
        ),
    ],
    ids=["missing", "null", "non_bool"],
)
def test_study_created_upcaster_require_hypotheses_refuses(
    payload: dict, match: str
) -> None:
    """Missing / null / non-bool → typed refuse (no invent False)."""
    with pytest.raises(CatalogError, match=match):
        upcast("StudyCreated", STUDY_CREATED_REQUIRE_HYPOTHESES_SINCE, payload)


def test_study_created_upcaster_require_hypotheses_keeps_bool() -> None:
    v, out = upcast(
        "StudyCreated",
        STUDY_CREATED_REQUIRE_HYPOTHESES_SINCE,
        {"study_id": "s", "catalog_version": 3, "require_hypotheses": True},
    )
    assert v == STUDY_CREATED_SCHEMA_VERSION
    assert out["require_hypotheses"] is True


def test_assert_profile_refuses_needed_false() -> None:
    with pytest.raises(PlanNeedRefuse, match="profile_requires_hypotheses") as ei:
        assert_profile_hypotheses_satisfied(_NEED_X, require_hypotheses=True)
    assert ei.value.reason == "profile_requires_hypotheses"
    assert ei.value.detail == _PROFILE_DETAIL


def test_assert_profile_refuses_need_unrecorded() -> None:
    with pytest.raises(PlanNeedRefuse, match=_UNRECORDED_DETAIL) as ei:
        assert_profile_hypotheses_satisfied(_UNREC, require_hypotheses=True)
    assert ei.value.reason == "plan_need_unrecorded"


def test_assert_profile_accepts_needed_true() -> None:
    assert_profile_hypotheses_satisfied(_NEED_ON, require_hypotheses=True)


def test_assert_profile_accepts_when_not_required() -> None:
    assert_profile_hypotheses_satisfied(_NEED_X, require_hypotheses=False)


@pytest.mark.asyncio
async def test_accept_plan_omit_refuses(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-omit")
    try:
        await rt.create_study(
            profile="t", request="q", catalog_version=3, require_hypotheses=False
        )
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
        await rt.create_study(
            profile="t", request="q", catalog_version=3, require_hypotheses=False
        )
        assert await rt.accept_plan([_EXP], hypotheses=NEED_OFF, literature=NEED_OFF)
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_profile_requires_hypotheses_refuses(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "g4-profile")
    try:
        await rt.create_study(
            profile="t", request="q", catalog_version=3, require_hypotheses=True
        )
        pid = await rt.accept_plan([_EXP], hypotheses=NEED_OFF, literature=NEED_OFF)
        assert pid == ""
        refused = [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
        assert len(refused) == 1
        assert refused[0].payload["detail"] == _PROFILE_DETAIL
    finally:
        await close_runtime(rt)
