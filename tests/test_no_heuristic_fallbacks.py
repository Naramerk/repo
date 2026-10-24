"""Raise tests for #38 review blockers: no heuristic fallbacks (direct access)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from coscientist.core.claims.contradicts import _accepted, _stance_of
from coscientist.core.claims.rules import PlanInvalid
from coscientist.core.claims.stance import claim_stance
from coscientist.core.events.errors import InvalidStatus, JournalFormatError
from coscientist.core.events.upcasters import require_exact_int
from coscientist.core.execution.domain import TaskState
from coscientist.core.plan.loop import (
    LOOP_REASON_ERROR,
    LOOP_REASON_ITERATION_CAP,
    capped_scope_questions,
    fold_loop_capped,
    fold_loop_completed,
    fold_loop_expansion_refused,
    fold_loop_refused,
)
from coscientist.core.plan.plan_validate import _has_decomposition_covers_rubric
from coscientist.core.plan.proposed_check import (
    assert_proposal_preregistered_for_eval,
    fold_proposed_check_event,
)
from coscientist.core.plan.share_alloc import (
    allocate_budget,
    enforce_task_budget_share,
    outstanding_task_allocations,
)
from coscientist.experimental.costly import CostlyEffect, costly_gate
from coscientist.experimental.hitl_costly import _decision_for_effect, _skip_reason


class _RT:
    """Minimal runtime stub for fold helpers."""

    def __init__(self) -> None:
        self._loop_state: dict = {}
        self._loop_capped: dict = {}
        self._loop_done: set = set()
        self._check_proposals: dict = {}
        self._proposed_check_reviews: dict = {}
        self.attempts: dict = {}
        self.tasks: dict = {}
        self._task_budget_alloc: dict = {}
        self._task_spent_rub: dict = {}
        self._study_spent_rub = Decimal("0")


# --- Blocker 1: fold_loop_capped / capped_scope_questions require reason ---


def test_blocker1_fold_loop_capped_missing_reason():
    with pytest.raises(JournalFormatError, match="missing 'reason'"):
        fold_loop_capped(_RT(), {"loop_id": "L1", "iter": 1})


def test_blocker1_fold_loop_capped_unknown_reason():
    with pytest.raises(JournalFormatError, match="unknown reason"):
        fold_loop_capped(
            _RT(), {"loop_id": "L1", "iter": 1, "reason": "not_a_reason"}
        )


def test_blocker1_capped_scope_questions_missing_reason():
    rt = _RT()
    rt._loop_capped = {"L1": {"iter": 2, "scope_questions": ["Q1"]}}
    with pytest.raises(JournalFormatError, match="missing 'reason'"):
        capped_scope_questions(rt)


def test_blocker1_capped_scope_questions_unknown_reason():
    rt = _RT()
    rt._loop_capped = {
        "L1": {"iter": 2, "reason": "weird", "scope_questions": ["Q1"]}
    }
    with pytest.raises(JournalFormatError, match="unknown reason"):
        capped_scope_questions(rt)


# --- Blocker 2: fold_loop_refused / completed / ExpansionRefused ---


def test_blocker2_fold_loop_refused_missing_reason():
    with pytest.raises(JournalFormatError, match="missing 'reason'"):
        fold_loop_refused(_RT(), {"loop_id": "L1", "iter": 0})


def test_blocker2_fold_loop_refused_unknown_reason():
    with pytest.raises(JournalFormatError, match="unknown reason"):
        fold_loop_refused(
            _RT(),
            {"loop_id": "L1", "iter": 0, "reason": LOOP_REASON_ITERATION_CAP},
        )


def test_blocker2_fold_loop_completed_missing_reason():
    with pytest.raises(JournalFormatError, match="missing 'reason'"):
        fold_loop_completed(_RT(), {"loop_id": "L1", "iter": 1})


def test_blocker2_fold_loop_completed_unknown_reason():
    with pytest.raises(JournalFormatError, match="unknown reason"):
        fold_loop_completed(
            _RT(), {"loop_id": "L1", "iter": 1, "reason": "done_somehow"}
        )


def test_blocker2_fold_expansion_refused_missing_loop_reason():
    with pytest.raises(JournalFormatError, match="missing 'loop_reason'"):
        fold_loop_expansion_refused(
            _RT(), {"loop": True, "loop_id": "L1", "detail": "x"}
        )


def test_blocker2_fold_expansion_refused_unknown_loop_reason():
    with pytest.raises(JournalFormatError, match="unknown loop_reason"):
        fold_loop_expansion_refused(
            _RT(),
            {"loop": True, "loop_id": "L1", "loop_reason": "mystery", "detail": "x"},
        )


# --- Blocker 3: exact int for loop iter ---


def test_blocker3_fold_loop_capped_missing_iter():
    with pytest.raises(JournalFormatError, match="missing 'iter'"):
        fold_loop_capped(
            _RT(), {"loop_id": "L1", "reason": LOOP_REASON_ITERATION_CAP}
        )


def test_blocker3_fold_loop_capped_bool_iter_rejected():
    with pytest.raises(JournalFormatError, match="exact int"):
        fold_loop_capped(
            _RT(),
            {
                "loop_id": "L1",
                "reason": LOOP_REASON_ITERATION_CAP,
                "iter": True,  # bool must not pass as int
            },
        )


def test_blocker3_fold_loop_capped_str_iter_rejected():
    with pytest.raises(JournalFormatError, match="exact int"):
        fold_loop_capped(
            _RT(),
            {
                "loop_id": "L1",
                "reason": LOOP_REASON_ERROR,
                "iter": "2",
            },
        )


def test_blocker3_capped_scope_no_iter_zero_fallback():
    rt = _RT()
    rt._loop_capped = {
        "L1": {
            "reason": LOOP_REASON_ITERATION_CAP,
            "scope_questions": ["Q1"],
        }
    }
    with pytest.raises(JournalFormatError, match="missing 'iter'"):
        capped_scope_questions(rt)


def test_blocker3_require_exact_int_rejects_bool():
    with pytest.raises(JournalFormatError, match="exact int"):
        require_exact_int(False, what="iter")


# --- Blocker 4: attempt_role required / unknown → InvalidStatus ---


@pytest.mark.asyncio
async def test_blocker4_attempt_role_required():
    from coscientist.core.plan.proposed_check import bind_proposed_check_reviewer

    rt = _RT()
    rt._check_proposals["p1"] = {
        "proposal_id": "p1",
        "status": "proposed",
        "proposer_attempt_id": "a_prop",
        "bound_task_id": "t_prop",
        "bound_target": "Q1",
    }
    rt._proposed_check_reviews["p1"] = {
        "status": "pending",
        "reviewer_task_id": "t_rev",
    }
    rt.attempts["a_rev"] = {"task_id": "t_rev"}  # no attempt_role
    with pytest.raises(InvalidStatus, match="attempt_role"):
        await bind_proposed_check_reviewer(rt, "p1", "a_rev")


@pytest.mark.asyncio
async def test_blocker4_attempt_role_unknown():
    from coscientist.core.plan.proposed_check import bind_proposed_check_reviewer

    rt = _RT()
    rt._check_proposals["p1"] = {
        "proposal_id": "p1",
        "status": "proposed",
        "proposer_attempt_id": "a_prop",
        "bound_task_id": "t_prop",
        "bound_target": "Q1",
    }
    rt._proposed_check_reviews["p1"] = {
        "status": "pending",
        "reviewer_task_id": "t_rev",
    }
    rt.attempts["a_rev"] = {"task_id": "t_rev", "attempt_role": "observer"}
    with pytest.raises(InvalidStatus, match="unknown attempt_role"):
        await bind_proposed_check_reviewer(rt, "p1", "a_rev")


# --- Blocker 5: status required (no pending default) ---


@pytest.mark.asyncio
async def test_blocker5_review_status_required_on_bind():
    from coscientist.core.plan.proposed_check import bind_proposed_check_reviewer

    rt = _RT()
    rt._check_proposals["p1"] = {
        "proposal_id": "p1",
        "status": "proposed",
        "proposer_attempt_id": "a_prop",
        "bound_task_id": "t_prop",
        "bound_target": "Q1",
    }
    rt._proposed_check_reviews["p1"] = {
        "reviewer_task_id": "t_rev",
        # status omitted
    }
    rt.attempts["a_rev"] = {"task_id": "t_rev", "attempt_role": "reviewer"}
    with pytest.raises(KeyError, match="status"):
        await bind_proposed_check_reviewer(rt, "p1", "a_rev")


def test_blocker5_fold_review_scheduled_requires_status():
    rt = _RT()
    ev = SimpleNamespace(
        type="ReviewScheduled",
        entity_key="p1",
        seq=1,
        payload={"kind": "proposed_check_review", "proposal_id": "p1"},
    )
    with pytest.raises(KeyError, match="status"):
        fold_proposed_check_event(rt, ev)


# --- Blocker 6: HitlDecided "by"; proposal proposer_attempt_id ---


def test_blocker6_hitl_decided_requires_by():
    from coscientist.core.plan import proposed_check as pc

    # Exercise the private verifier used by approve/reject human paths.
    rt = _RT()
    rt.hitl = {
        "h1": {"kind": "proposed_check", "payload_ref": "p1"},
    }
    rt._hitl_decisions = {
        "h1": {
            "decision": "accept",
            "proposal_id": "p1",
            # "by" missing — decided_by must not be a fallback
            "decided_by": "human",
        }
    }
    with pytest.raises(KeyError, match="by"):
        pc._verify_human_principal(  # noqa: SLF001
            rt,
            source="hitl",
            proposal_id_s="p1",
            hitl_request_id_s="h1",
            human_principal=None,
        )


@pytest.mark.asyncio
async def test_blocker6_proposer_attempt_id_no_attempt_id_fallback():
    from coscientist.core.plan.proposed_check import bind_proposed_check_reviewer

    rt = _RT()
    rt._check_proposals["p1"] = {
        "proposal_id": "p1",
        "status": "proposed",
        "attempt_id": "a_prop",  # legacy key only — must not satisfy
        "bound_task_id": "t_prop",
        "bound_target": "Q1",
    }
    rt._proposed_check_reviews["p1"] = {
        "status": "pending",
        "reviewer_task_id": "t_rev",
    }
    rt.attempts["a_rev"] = {"task_id": "t_rev", "attempt_role": "reviewer"}
    with pytest.raises(KeyError, match="proposer_attempt_id"):
        await bind_proposed_check_reviewer(rt, "p1", "a_rev")


# --- Blocker 7: bound_* only (no task_id/target fallback) ---


def test_blocker7_prereg_requires_bound_keys():
    row = {
        "task_id": "t1",
        "target": "Q1",
        "proposed_seq": 1,
        # bound_* missing after fold should have filled them
    }
    err = assert_proposal_preregistered_for_eval(
        row,
        attempt_id="a1",
        task_id="t1",
        target="Q1",
        attempts={"a1": {"_seq": 2, "task_id": "t1"}},
        results={},
    )
    assert err is not None
    assert "bound" in err


def test_blocker7_check_proposed_fold_fills_bound_from_task_target():
    rt = _RT()
    ev = SimpleNamespace(
        type="CheckProposed",
        entity_key="p1",
        seq=3,
        payload={
            "proposal_id": "p1",
            "proposal_digest": "d",
            "task_id": "t1",
            "target": "Q1",
            "proposer_attempt_id": "a1",
        },
    )
    fold_proposed_check_event(rt, ev)
    row = rt._check_proposals["p1"]
    assert row["bound_task_id"] == "t1"
    assert row["bound_target"] == "Q1"


def test_blocker7_check_approved_fold_requires_bound_on_payload():
    rt = _RT()
    rt._check_proposals["p1"] = {
        "proposal_id": "p1",
        "status": "proposed",
        "proposal_digest": "d",
        "bound_task_id": "t1",
        "bound_target": "Q1",
        "task_id": "t1",
        "target": "Q1",
    }
    ev = SimpleNamespace(
        type="CheckApproved",
        entity_key="p1",
        seq=4,
        payload={
            "proposal_id": "p1",
            "proposal_digest": "d",
            "by": "human",
            # bound_* omitted — must not fall back to task_id/target on row
        },
    )
    with pytest.raises(KeyError, match="bound_"):
        fold_proposed_check_event(rt, ev)


# --- Blocker 8: ValidationError → PlanInvalid ---


def test_blocker8_bad_rubric_raises_plan_invalid():
    # Bypass OutputSpec construction so Rubric.model_validate is what fails.
    spec = SimpleNamespace(
        expected_outputs=[SimpleNamespace(rubric={"not": "a valid rubric"})]
    )
    with pytest.raises(PlanInvalid):
        _has_decomposition_covers_rubric([spec])  # type: ignore[list-item]


# --- Blocker 9: contradicts strict attrs ---


def test_blocker9_stance_requires_criterion_status():
    with pytest.raises(KeyError, match="criterion_status"):
        _stance_of({"accepted": True, "frozen": True})


def test_blocker9_stance_unknown_raises():
    with pytest.raises(KeyError):
        claim_stance("maybe")


def test_blocker9_frozen_no_default():
    with pytest.raises(KeyError, match="frozen"):
        _accepted({"accepted": True, "criterion_status": "met"})


def test_blocker9_accepted_ok():
    assert _accepted({"accepted": True, "frozen": True}) is True
    assert _stance_of({"criterion_status": "met"}) == "supports"


# --- Blocker 10: costly_gate structured CostlyEffect ---


def test_blocker10_costly_gate_returns_enum():
    eff, ev = costly_gate("fail", "approve", 0.0, None)
    assert eff is CostlyEffect.RUN
    assert ev["decided_by"] == "human"


def test_blocker10_skip_reason_structured():
    assert _skip_reason(CostlyEffect.SKIPPED_BUDGET) == "budget"
    assert _skip_reason(CostlyEffect.SKIPPED_HUMAN_REJECTED) == "human_rejected"
    with pytest.raises(ValueError, match="not a SKIPPED"):
        _skip_reason(CostlyEffect.RUN)


def test_blocker10_decision_unknown_effect_raises():
    # Construct a fake via string that is not a known member used wrongly
    with pytest.raises(ValueError, match="unknown CostlyEffect"):
        _decision_for_effect("NOT_AN_EFFECT")  # type: ignore[arg-type]


# --- Blocker 11: share_alloc direct map access ---


def test_blocker11_shares_direct_access_for_plan_key():
    from datetime import datetime, timezone

    from coscientist.core.execution.domain import Budget, HitlPolicy, Study, TaskSpec

    study = Study(
        study_id="s",
        profile="t",
        request="q",
        catalog_version="1",
        hitl_policy=HitlPolicy(),
        created_at=datetime.now(timezone.utc),
        deadline=datetime(2099, 1, 1, tzinfo=timezone.utc),
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    plan = [
        TaskSpec(
            task_key="E0",
            title="t",
            goal="g",
            route="experiment",
            budget_share=1.0,
        )
    ]
    got = allocate_budget(
        "E0",
        study=study,
        spent_rub=Decimal("0"),
        plan_tasks=plan,
        not_started={"E0"},
    )
    assert got is not None and got > 0
    # Non-plan key: no cap (None), not a silent zero share.
    assert (
        allocate_budget(
            "MISSING",
            study=study,
            spent_rub=Decimal("0"),
            plan_tasks=plan,
            not_started={"E0"},
        )
        is None
    )


@pytest.mark.asyncio
async def test_blocker11_spent_required_when_cap_exists():
    rt = _RT()
    rt._task_budget_alloc = {"t1": Decimal("10")}
    # spent entry missing
    rt.tasks["t1"] = SimpleNamespace(state=TaskState.PENDING, task_id="t1")

    async def _cancel(*_a, **_k):
        return None

    rt.cancel_task = _cancel  # type: ignore[method-assign]
    with pytest.raises(KeyError):
        await enforce_task_budget_share(rt, "t1")


def test_blocker11_outstanding_requires_spent_for_cap():
    rt = _RT()
    rt._task_budget_alloc = {"t1": Decimal("10")}
    rt.tasks["t1"] = SimpleNamespace(state=TaskState.PENDING, task_id="t1")
    with pytest.raises(KeyError):
        outstanding_task_allocations(rt)


def test_blocker11_none_cap_still_skips():
    """Present map entry with None means no cap — must not invent spend."""
    rt = _RT()
    rt._task_budget_alloc = {"t1": None}
    rt.tasks["t1"] = SimpleNamespace(state=TaskState.PENDING, task_id="t1")
    assert outstanding_task_allocations(rt) == Decimal("0")
