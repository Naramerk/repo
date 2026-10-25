"""RC4 (#100): HYP-n when Study.require_hypotheses / Need.needed."""

from __future__ import annotations

import pytest

from coscientist.core.execution.domain import Need, TaskKind, TaskSpec
from coscientist.core.plan.expand_validate import task_kind_for_route
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


def test_assert_hyp_n_when_needed_unit() -> None:
    assert_hyp_n_when_needed([_EXP], NEED_OFF)
    assert_hyp_n_when_needed([_HYP_N, _EXP], _NEED_ON)
    with pytest.raises(PlanNeedRefuse, match=_DETAIL) as ei:
        assert_hyp_n_when_needed([_EXP], _NEED_ON)
    assert ei.value.reason == "hyp_n_required"


@pytest.mark.asyncio
async def test_require_hypotheses_forces_hyp_n(tmp_path) -> None:
    """DoD: require_hypotheses=True → plan without HYP-n is PlanRefused."""
    rt = await open_runtime(tmp_path, "rc4-force")
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
        assert pid == ""
        refused = [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
        assert len(refused) == 1
        assert refused[0].payload["detail"] == _DETAIL
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
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_need_alone_forces_hyp_n_when_flag_false(tmp_path) -> None:
    """RC4: require_hypotheses=false → Need.needed alone still requires HYP-n."""
    rt = await open_runtime(tmp_path, "rc4-need")
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
        assert pid == ""
        refused = [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
        assert len(refused) == 1
        assert refused[0].payload["detail"] == _DETAIL
    finally:
        await close_runtime(rt)
