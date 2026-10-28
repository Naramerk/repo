"""RC4 (#100): HYP-n whenever the plan Need is on — launch flag or planner-set.

The system auto-insert covers both (one invariant, one rule); Need off stays
insert-free and refuse-free."""

from __future__ import annotations

import pytest

from coscientist.core.execution.domain import Need, TaskKind, TaskSpec
from coscientist.core.plan.expand_validate import task_kind_for_route
from coscientist.core.plan.lifecycle import (
    HYP_STAGE_GOAL,
    HYP_STAGE_TITLE,
    _next_hyp_task_key,
    hyp_auto_insert_note,
)
from coscientist.core.plan.need import (
    PlanNeedRefuse,
    assert_hyp_n_when_needed,
    is_hyp_n_task_key,
)
from tests.helpers import NEED_OFF, close_runtime, open_runtime

_NEED_ON = Need(kind="need", needed=True, reason="hypotheses required")
_HYP_N = TaskSpec(
    task_key="HYP-0",
    title="hypotheses",
    goal="propose hypotheses",
    route="hypotheses",
)
_EXP = TaskSpec(task_key="EXP-1", title="e", goal="e", route="experiment")
_DETAIL = "hyp_n_required:plan_must_include_HYP-n"


def test_is_hyp_n_task_key() -> None:
    assert is_hyp_n_task_key("HYP-0")
    assert is_hyp_n_task_key("HYP-12")
    assert not is_hyp_n_task_key("HYP")
    assert not is_hyp_n_task_key("EXP-0")
    assert not is_hyp_n_task_key("hyp-0")


def test_next_hyp_task_key_skips_taken_keys() -> None:
    assert _next_hyp_task_key(set()) == "HYP-1"
    assert _next_hyp_task_key({"HYP-1"}) == "HYP-2"
    assert _next_hyp_task_key({"HYP-1", "HYP-2", "EXP-9"}) == "HYP-3"
    # Stage key "HYP" (no number) never collides with canonical HYP-n keys.
    assert _next_hyp_task_key({"HYP"}) == "HYP-1"


def test_hyp_auto_insert_note_names_the_key() -> None:
    note = hyp_auto_insert_note("HYP-1")
    assert "HYP-1" in note
    assert "инвариант стадии" in note
    # Flag-neutral wording: the note states the active need (the invariant),
    # never the launch flag — the same insert serves flag-off studies where
    # the planner itself set Need.needed.
    assert "require_hypotheses" not in note


def test_assert_hyp_n_when_needed_unit() -> None:
    assert_hyp_n_when_needed([_EXP], NEED_OFF)
    assert_hyp_n_when_needed([_HYP_N, _EXP], _NEED_ON)
    with pytest.raises(PlanNeedRefuse, match=_DETAIL) as ei:
        assert_hyp_n_when_needed([_EXP], _NEED_ON)
    assert ei.value.reason == "hyp_n_required"


@pytest.mark.asyncio
async def test_require_hypotheses_auto_inserts_hyp_n(tmp_path) -> None:
    """RC4 system invariant: require_hypotheses=True + plan without HYP-n → the
    ACCEPT PATH inserts the stage task instead of refusing (live walls
    st_idem_1ea2 / st_idem_932e ignored even the visible #306 pin — the
    invariant is guaranteed by the system, not the model)."""
    rt = await open_runtime(tmp_path, "rc4-auto")
    try:
        await rt.create_study(
            profile="t",
            request="q",
            catalog_version=4,
            require_hypotheses=True,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            inputs=[],
        )
        pid = await rt.accept_plan(
            [_EXP],
            questions=[],
            hypotheses=_NEED_ON,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid
        # Inserted stage task: hypotheses kind + intake stage conventions.
        hyp = next(t for t in rt.tasks.values() if t.spec.task_key == "HYP-1")
        assert hyp.kind == TaskKind.HYPOTHESES
        assert hyp.spec.required is False
        assert hyp.spec.criteria == []
        assert hyp.spec.title == HYP_STAGE_TITLE
        assert hyp.spec.goal == HYP_STAGE_GOAL
        # Journaled + visible: readable system note on PlanAccepted itself.
        accepted = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        assert len(accepted) == 1
        note = accepted[0].payload["note"]
        assert isinstance(note, str) and "HYP-1" in note
        assert "инвариант стадии" in note
        # The inserted task is part of the accepted plan payload.
        assert any(t["task_key"] == "HYP-1" for t in accepted[0].payload["tasks"])
        # The plans projection row carries the same readable note.
        plan_rows = [r for r in await rt.store.get_projection("plans") if r.get("plan_id") == pid]
        assert len(plan_rows) == 1
        assert plan_rows[0]["note"] == note
        assert not [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_require_hypotheses_accepts_with_hyp_n(tmp_path) -> None:
    rt = await open_runtime(tmp_path, "rc4-ok")
    try:
        await rt.create_study(
            profile="t",
            request="q",
            catalog_version=4,
            require_hypotheses=True,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            inputs=[],
        )
        pid = await rt.accept_plan(
            [_HYP_N, _EXP],
            questions=[],
            hypotheses=_NEED_ON,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid
        hyp = next(t for t in rt.tasks.values() if t.spec.task_key == "HYP-0")
        assert hyp.kind == TaskKind.HYPOTHESES
        assert task_kind_for_route("hypotheses") == TaskKind.HYPOTHESES
        # Planner-provided stage task → no system insert, no note (payload
        # parity with v12: the writer omits the key entirely).
        accepted = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        assert len(accepted) == 1
        assert "note" not in accepted[0].payload
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_planner_need_auto_inserts_hyp_n_when_flag_false(tmp_path) -> None:
    """THE fixed case: require_hypotheses=false + planner-set Need.needed=true
    + zero HYP-n → the ACCEPT PATH inserts the stage task (was a
    ``hyp_n_required`` refuse). Live st_idem_d893027 died here because the
    #312 insert keyed on the launch flag while the refuse keyed on the plan
    Need; the invariant is one rule regardless of who turned the Need on."""
    rt = await open_runtime(tmp_path, "rc4-need-insert")
    try:
        await rt.create_study(
            profile="t",
            request="q",
            catalog_version=4,
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            inputs=[],
        )
        pid = await rt.accept_plan(
            [_EXP],
            questions=[],
            hypotheses=_NEED_ON,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid
        # Inserted stage task: hypotheses kind + intake stage conventions.
        hyp = next(t for t in rt.tasks.values() if t.spec.task_key == "HYP-1")
        assert hyp.kind == TaskKind.HYPOTHESES
        assert hyp.spec.required is False
        assert hyp.spec.criteria == []
        assert hyp.spec.title == HYP_STAGE_TITLE
        assert hyp.spec.goal == HYP_STAGE_GOAL
        # Journaled + visible: readable system note on PlanAccepted itself.
        accepted = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        assert len(accepted) == 1
        note = accepted[0].payload["note"]
        assert isinstance(note, str) and "HYP-1" in note
        assert "инвариант стадии" in note
        assert "require_hypotheses" not in note
        # The inserted task is part of the accepted plan payload.
        assert any(t["task_key"] == "HYP-1" for t in accepted[0].payload["tasks"])
        assert not [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_need_off_no_hyp_task_no_insert_no_refuse(tmp_path) -> None:
    """Honest backstop parity: Need off + zero HYP-n → no insert, no refuse."""
    rt = await open_runtime(tmp_path, "rc4-need-off")
    try:
        await rt.create_study(
            profile="t",
            request="q",
            catalog_version=4,
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            inputs=[],
        )
        pid = await rt.accept_plan(
            [_EXP],
            questions=[],
            hypotheses=NEED_OFF,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid
        assert not [t for t in rt.tasks.values() if is_hyp_n_task_key(t.spec.task_key)]
        accepted = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        assert len(accepted) == 1
        assert "note" not in accepted[0].payload
        assert not [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_need_on_with_planner_hyp_n_flag_false_untouched(tmp_path) -> None:
    """Planner-provided HYP-n with the flag off → untouched: no insert, no note."""
    rt = await open_runtime(tmp_path, "rc4-need-hyp")
    try:
        await rt.create_study(
            profile="t",
            request="q",
            catalog_version=4,
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            inputs=[],
        )
        pid = await rt.accept_plan(
            [_HYP_N, _EXP],
            questions=[],
            hypotheses=_NEED_ON,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid
        assert not any(t.spec.task_key == "HYP-1" for t in rt.tasks.values())
        accepted = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        assert len(accepted) == 1
        assert "note" not in accepted[0].payload
    finally:
        await close_runtime(rt)
