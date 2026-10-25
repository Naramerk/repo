"""RC7r (#103): HITL reject → status rejected; no PlanAccepted."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.execution.domain import QuestionSpec, TaskSpec
from coscientist.core.plan.need import (
    PlanNeedRefuse,
    assert_hitl_reject_blocks_plan_accepted,
    hitl_reject_status,
    hypotheses_hitl_reject_status,
)
from coscientist.experimental.runtime import StudyRuntime as ExpStudyRuntime
from tests.helpers import NEED_OFF, attach_executor, close_runtime, open_runtime

_DETAIL = "hitl_reject:no_plan_accepted"


def test_reject_writes_no_plan_accepted() -> None:
    """DoD: HITL reject records status rejected and forbids PlanAccepted."""
    status = hitl_reject_status("reject")
    assert status == "rejected"
    with pytest.raises(PlanNeedRefuse) as ei:
        assert_hitl_reject_blocks_plan_accepted(status=status)
    assert ei.value.reason == "hitl_reject"
    assert str(ei.value) == _DETAIL
    with pytest.raises(PlanNeedRefuse) as ei2:
        assert_hitl_reject_blocks_plan_accepted(decision="reject")
    assert ei2.value.reason == "hitl_reject"
    assert str(ei2.value) == _DETAIL


def test_hitl_reject_status_refuses_non_reject() -> None:
    with pytest.raises(PlanNeedRefuse) as ei:
        hitl_reject_status("accept")
    assert ei.value.reason == "invalid"
    assert "expected_reject" in str(ei.value)


def test_hypotheses_hitl_reject_status_from_folded_maps() -> None:
    opens = {"rid1": {"kind": "hypotheses", "open": False}}
    decisions = {"rid1": {"decision": "reject", "by": "human"}}
    assert hypotheses_hitl_reject_status(hitl_opens=opens, hitl_decisions=decisions) == (
        "rejected"
    )
    assert (
        hypotheses_hitl_reject_status(
            hitl_opens={"rid1": {"kind": "plan_approval"}},
            hitl_decisions=decisions,
        )
        is None
    )
    assert (
        hypotheses_hitl_reject_status(
            hitl_opens=opens,
            hitl_decisions={"rid1": {"decision": "accept"}},
        )
        is None
    )
    assert hypotheses_hitl_reject_status(hitl_opens=None, hitl_decisions=None) is None


def test_accept_without_hitl_reject_allowed() -> None:
    assert_hitl_reject_blocks_plan_accepted(decision="accept", status=None)
    assert_hitl_reject_blocks_plan_accepted()


@pytest.mark.asyncio
async def test_accept_plan_refuses_after_hypotheses_hitl_reject(tmp_path) -> None:
    """Refuse/restore: folded hypotheses HITL reject → accept_plan writes no PlanAccepted."""
    rt = await open_runtime(
        tmp_path, "rc7r", runtime_cls=ExpStudyRuntime
    )
    try:
        await rt.create_study(
            profile="t",
            request="q",
            catalog_version=4,
            deadline=rt.clock.now() + timedelta(seconds=400),
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            inputs=[],
        )
        _, arts = attach_executor(rt, tmp_path)
        # SPEC boot so PLAN/HITL task keys resolve.
        from tests.helpers import record_success

        aid = await rt.start_attempt("SPEC")
        await record_success(rt, aid, arts)

        rid = await rt.open_hitl("PLAN", kind="hypotheses", timeout_s=60)
        await rt.answer_hitl(rid, "reject")
        assert (
            hypotheses_hitl_reject_status(
                hitl_opens=rt.hitl,
                hitl_decisions=rt._hitl_decisions,
            )
            == "rejected"
        )

        before = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        refused_id = await rt.accept_plan(
            [TaskSpec(task_key="EXP-1", title="e", goal="e", route="experiment")],
            questions=[QuestionSpec(id="Q1", text="q")],
            hypotheses=NEED_OFF,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        # apply_plan_refused returns "" — never a PlanAccepted plan_id.
        assert refused_id == ""
        after = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        assert after == before
        refused = [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
        assert refused
        assert _DETAIL in str(refused[-1].payload.get("detail") or "")
    finally:
        await close_runtime(rt)
