"""RC7r (#103): HITL reject → status rejected; no PlanAccepted."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.projections import apply_to_map, fold_events
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.domain import Need, TaskSpec
from coscientist.core.plan.need import (
    PlanNeedRefuse,
    assert_hitl_reject_blocks_plan,
    stamp_hitl_reject_status,
)
from tests.helpers import NEED_OFF, close_runtime, open_runtime

_DETAIL_PREFIX = "hitl_reject_blocks_plan:"
_RID = "hitl_rc7r_1"


def test_stamp_hitl_reject_status_for_hypotheses() -> None:
    out = stamp_hitl_reject_status(
        {"request_id": _RID, "kind": "hypotheses", "open": False},
        decision="reject",
    )
    assert out["status"] == "rejected"
    assert out["open"] is False


def test_stamp_hitl_reject_status_skips_non_plan_gate() -> None:
    out = stamp_hitl_reject_status(
        {"request_id": _RID, "kind": "proposed_check", "open": False},
        decision="reject",
    )
    assert "status" not in out


def test_assert_hitl_reject_blocks_plan_unit() -> None:
    with pytest.raises(PlanNeedRefuse) as ei:
        assert_hitl_reject_blocks_plan(
            hitl_requests={_RID: {"kind": "hypotheses", "request_id": _RID}},
            hitl_decisions={_RID: {"decision": "reject", "request_id": _RID}},
        )
    assert ei.value.reason == "hitl_reject_blocks_plan"
    assert str(ei.value).startswith(_DETAIL_PREFIX)
    assert "kind=hypotheses" in str(ei.value)

    assert_hitl_reject_blocks_plan(
        hitl_requests={_RID: {"kind": "hypotheses"}},
        hitl_decisions={_RID: {"decision": "accept"}},
    )
    assert_hitl_reject_blocks_plan(
        hitl_requests={_RID: {"kind": "budget"}},
        hitl_decisions={_RID: {"decision": "reject"}},
    )


def test_projection_reject_sets_status_rejected() -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    opened = EventEnvelope(
        study_id="st",
        type="HitlOpened",
        entity_key=_RID,
        idem_key=make_idem("HitlOpened", _RID),
        payload={
            "request_id": _RID,
            "kind": "plan_approval",
            "open": True,
            "task_id": "PLAN",
            "cost_rub": 0.0,
            "payload_ref": None,
            "deadline": None,
            "study_id": "st",
            "expand_after_task": None,
        },
        ts=now,
        schema_version=writer_schema_version("HitlOpened"),
    )
    decided = EventEnvelope(
        study_id="st",
        type="HitlDecided",
        entity_key=_RID,
        idem_key=make_idem("HitlDecided", _RID),
        payload={
            "request_id": _RID,
            "decision": "reject",
            "by": "human",
            "reason": "answered",
            "spent_rub": 0.0,
        },
        ts=now,
        schema_version=writer_schema_version("HitlDecided"),
    )
    proj: dict = {}
    apply_to_map(proj, opened)
    apply_to_map(proj, decided)
    row = proj["hitl_requests"][_RID]
    assert row["status"] == "rejected"
    assert row["open"] is False
    assert fold_events([opened, decided])["hitl_requests"][_RID]["status"] == "rejected"


@pytest.mark.asyncio
async def test_reject_writes_no_plan_accepted(tmp_path) -> None:
    """DoD: plan-gate HITL reject ⇒ accept_plan writes PlanRefused, not PlanAccepted."""
    rt = await open_runtime(tmp_path, "rc7r-reject")
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
        sid = rt.study.study_id
        now = rt.clock.now()
        await rt.append_events(
            [
                EventEnvelope(
                    study_id=sid,
                    type="HitlOpened",
                    entity_key=_RID,
                    idem_key=make_idem("HitlOpened", _RID),
                    payload={
                        "request_id": _RID,
                        "kind": "hypotheses",
                        "open": True,
                        "task_id": "HYP",
                        "cost_rub": 0.0,
                        "payload_ref": "hyps",
                        "deadline": now.isoformat(),
                        "study_id": sid,
                        "expand_after_task": None,
                    },
                    ts=now,
                    schema_version=writer_schema_version("HitlOpened"),
                ),
                EventEnvelope(
                    study_id=sid,
                    type="HitlDecided",
                    entity_key=_RID,
                    idem_key=make_idem("HitlDecided", _RID),
                    payload={
                        "request_id": _RID,
                        "decision": "reject",
                        "by": "human",
                        "decided_by": "human",
                        "approved_by": "human",
                        "reason": "answered",
                        "spent_rub": 0.0,
                    },
                    ts=now,
                    schema_version=writer_schema_version("HitlDecided"),
                ),
            ]
        )
        assert rt._hitl_requests[_RID].get("status") == "rejected"

        pid = await rt.accept_plan(
            [TaskSpec(task_key="EXP-1", title="e", goal="e", route="experiment")],
            questions=[],
            hypotheses=Need(kind="need", needed=False, reason="hypotheses rejected"),
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid == ""
        events = await rt.store.read_all()
        assert not any(e.type == "PlanAccepted" for e in events)
        refused = [e for e in events if e.type == "PlanRefused"]
        assert len(refused) == 1
        assert refused[0].payload["detail"].startswith(_DETAIL_PREFIX)
    finally:
        await close_runtime(rt)
