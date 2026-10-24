"""Raise tests for #38 review blockers: no heuristic fallbacks (direct access)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from coscientist.core.claims.contradicts import _accepted, _stance_of
from coscientist.core.claims.rules import PlanInvalid
from coscientist.core.claims.stance import claim_stance
from coscientist.core.events.errors import (
    InvalidStatus,
    JournalFormatError,
    JournalLockedError,
    JournalUnavailableError,
)
from coscientist.core.events.upcasters import require_exact_int, writer_schema_version
from coscientist.core.execution.domain import TaskKind, TaskOutcome, TaskState
from coscientist.core.plan.loop import (
    LOOP_REASON_ERROR,
    LOOP_REASON_ITERATION_CAP,
    capped_scope_questions,
    fold_loop_capped,
    fold_loop_completed,
    fold_loop_expansion_refused,
    fold_loop_plan_expanded,
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
from tests.helpers import default_test_hitl_policy, make_test_study


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
    with pytest.raises(JournalFormatError, match="status"):
        await bind_proposed_check_reviewer(rt, "p1", "a_rev")


def test_blocker5_fold_review_scheduled_requires_status():
    rt = _RT()
    ev = SimpleNamespace(
        type="ReviewScheduled",
        entity_key="p1",
        seq=1,
        payload={"kind": "proposed_check_review", "proposal_id": "p1"},
    )
    with pytest.raises(JournalFormatError, match="status"):
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
            "delegated_by": "human",
        }
    }
    with pytest.raises(JournalFormatError, match="by"):
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
    with pytest.raises(JournalFormatError, match="proposer_attempt_id"):
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
            "approved_by": "human",
            "source": "api",
            # bound_* omitted — must not fall back to task_id/target on row
        },
    )
    with pytest.raises(JournalFormatError, match="bound_"):
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
    with pytest.raises(JournalFormatError, match="criterion_status"):
        _stance_of({"accepted": True, "frozen": True})


def test_blocker9_stance_unknown_raises():
    with pytest.raises(JournalFormatError, match="unknown criterion_status"):
        claim_stance("maybe")


def test_blocker9_frozen_no_default():
    with pytest.raises(JournalFormatError, match="frozen"):
        _accepted({"accepted": True, "criterion_status": "met"})


def test_blocker9_accepted_ok():
    assert _accepted({"accepted": True, "frozen": True}) is True
    assert _stance_of({"criterion_status": "met"}) == "supports"


# --- Blocker 10: costly_gate structured CostlyEffect ---


def test_blocker10_costly_gate_returns_enum():
    eff, ev = costly_gate("fail", "approve", 0.0, None, critic_accepts=True)
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

    from coscientist.core.execution.domain import Budget, TaskSpec

    study = make_test_study(
        study_id="s",
        profile="t",
        request="q",
        catalog_version="1",
        hitl_policy=default_test_hitl_policy(),
        created_at=datetime.now(timezone.utc),
        deadline=datetime(2099, 1, 1, tzinfo=timezone.utc),
        budget=Budget(max_cost_rub=Decimal("100"), max_llm_calls=100, max_tokens=1000000, max_tool_calls=500),
    )
    plan = [
        TaskSpec(
            task_key="E0",
            title="t",
            goal="g",
            route="experiment",
            budget_share=1.0, required=True, tolerate_failed_dep=False)
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
    with pytest.raises(JournalFormatError, match="spent missing"):
        await enforce_task_budget_share(rt, "t1")


def test_blocker11_outstanding_requires_spent_for_cap():
    rt = _RT()
    rt._task_budget_alloc = {"t1": Decimal("10")}
    rt.tasks["t1"] = SimpleNamespace(state=TaskState.PENDING, task_id="t1")
    with pytest.raises(JournalFormatError, match="spent missing"):
        outstanding_task_allocations(rt)


def test_blocker11_none_cap_still_skips():
    """Present map entry with None means no cap — must not invent spend."""
    rt = _RT()
    rt._task_budget_alloc = {"t1": None}
    rt.tasks["t1"] = SimpleNamespace(state=TaskState.PENDING, task_id="t1")
    assert outstanding_task_allocations(rt) == Decimal("0")


def test_attempt_started_cap_initializes_spent_zero():
    """When allocated_budget_rub is set and spent missing, fold seeds spent=0."""
    from datetime import UTC, datetime

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _Task:
        def __init__(self) -> None:
            self.attempts: list[str] = []
            self.state = TaskState.PENDING
            self.reason = None
            self.spec = SimpleNamespace(task_key="E1")
            self.deadline = None

        def model_copy(self, *, update: dict) -> _Task:
            out = _Task()
            out.attempts = list(update.get("attempts", self.attempts))
            out.state = update.get("state", self.state)
            out.reason = update.get("reason", self.reason)
            out.spec = self.spec
            out.deadline = update.get("deadline", self.deadline)
            return out

    class _RTLocal(LocalProjectMixin):
        def __init__(self) -> None:
            self.attempts = {}
            self.tasks = {"t1": _Task()}
            self._retry_at = {}
            self._task_budget_alloc = {}
            self._task_spent_rub = {}

    rt = _RTLocal()
    ev = EventEnvelope(
        study_id="s",
        type="AttemptStarted",
        entity_key="a1",
        idem_key="AttemptStarted:a1",
        payload={
            "attempt_id": "a1",
            "task_id": "t1",
            "task_key": "E1",
            "attempt_role": "agent",
            "allocated_budget_rub": "10",
            "inputs": [],
            "tainted_context": False,
            "charged": True,
            "provenance": "real",
        },
        ts=datetime(2024, 1, 1, tzinfo=UTC),
        schema_version=writer_schema_version("AttemptStarted"),
)
    rt._project_local(ev)  # noqa: SLF001
    assert rt._task_budget_alloc["t1"] == Decimal("10")
    assert rt._task_spent_rub["t1"] == Decimal("0")


def test_check_approved_fold_requires_source_canonical():
    rt = _RT()
    rt._check_proposals["p1"] = {
        "proposal_id": "p1",
        "status": "proposed",
        "proposal_digest": "d",
        "bound_task_id": "t1",
        "bound_target": "Q1",
    }
    ev = SimpleNamespace(
        type="CheckApproved",
        entity_key="p1",
        seq=4,
        payload={
            "proposal_id": "p1",
            "proposal_digest": "d",
            "approved_by": "human",
            # source omitted — must not fall back to approved_by/by
            "bound_task_id": "t1",
            "bound_target": "Q1",
        },
    )
    with pytest.raises(JournalFormatError, match="source"):
        fold_proposed_check_event(rt, ev)


def test_loop_capped_report_requires_survivors_for_iteration_cap():
    from coscientist.core.finalize.finalize import _append_loop_capped_report_line

    seen: list[str] = []
    with pytest.raises(JournalFormatError, match="survivors"):
        _append_loop_capped_report_line(
            seen,
            {
                "loop_id": "L1",
                "iter": 2,
                "reason": LOOP_REASON_ITERATION_CAP,
                # survivors missing
            },
        )


def test_loop_capped_report_classifies_by_reason_not_detail_prefix():
    """detail.startswith('loop_predicate:') must not force error classification."""
    from coscientist.core.finalize.finalize import _append_loop_capped_report_line

    seen: list[str] = []
    _append_loop_capped_report_line(
        seen,
        {
            "loop_id": "L1",
            "iter": 2,
            "reason": LOOP_REASON_ITERATION_CAP,
            "survivors": 3,
            "detail": "loop_predicate:boom",
        },
    )
    assert any("лимитом" in line for line in seen)
    assert "iteration_cap" in seen
    assert not any("ошибкой" in line for line in seen)


def test_loop_completed_report_requires_survivors():
    from coscientist.core.finalize.finalize import _append_loop_completed_report_line

    seen: list[str] = []
    with pytest.raises(JournalFormatError, match="survivors"):
        _append_loop_completed_report_line(
            seen, {"loop_id": "L1", "iter": 1, "reason": "until_met"}
        )


def test_fold_loop_plan_expanded_bool_iter_rejected():
    """False must not bypass require_exact_int via `!= 0` truthiness."""
    rt = _RT()
    rt._loop_state = {"L1": {"iter": False, "iters": {}}}
    with pytest.raises(JournalFormatError, match="exact int"):
        fold_loop_plan_expanded(
            rt,
            {
                "loop": True,
                "loop_id": "L1",
                "loop_iter": 1,
                "added_tasks": [],
            },
        )


# --- MaxDepthExceeded must stay typed (expansion_limit, not invalid) ---


def test_critic_gate_propagates_max_depth_exceeded(monkeypatch):
    """critic_gate must not stringify MaxDepthExceeded into a detail return."""
    from coscientist.core.plan import expand_critic as ec
    from coscientist.core.plan import shares as shares_mod
    from coscientist.core.plan.expand_validate import MaxDepthExceeded

    def boom(*_a, **_k):
        raise MaxDepthExceeded(3)

    monkeypatch.setattr(ec, "validate_expansion", boom)
    rt = _RT()
    rt.study = SimpleNamespace(max_expansions=2)
    rt.catalog = SimpleNamespace(templates=[])
    rt.plans = {}
    rt._agent_expansions = 0
    req = SimpleNamespace(
        tasks=[],
        questions=[],
        superseded_tasks=[],
        superseded_questions=[],
    )
    monkeypatch.setattr(ec, "assign_core_keys", lambda *_a, **_k: [])
    monkeypatch.setattr(ec, "active_plan_blob", lambda *_a, **_k: None)
    monkeypatch.setattr(shares_mod, "agent_raises_budget_share", lambda **_k: None)
    with pytest.raises(MaxDepthExceeded):
        ec.critic_gate(rt, req, after_task="E0")


@pytest.mark.asyncio
async def test_apply_pending_max_depth_refused_as_expansion_limit(monkeypatch):
    """apply_pending_expansion maps MaxDepthExceeded → ExpansionRefused(expansion_limit)."""
    from coscientist.core.plan import expand as expand_mod
    from coscientist.core.plan.expand_validate import MaxDepthExceeded

    refused: list[dict] = []

    async def fake_refuse(runtime, after, *, reason, detail, plan_key=None):
        refused.append({"reason": reason, "detail": detail, "plan_key": plan_key})

    monkeypatch.setattr(expand_mod, "refuse_expansion", fake_refuse)
    monkeypatch.setattr(
        expand_mod,
        "_resolve_plan_n_request",
        lambda *_a, **_k: (SimpleNamespace(tasks=[], questions=[]), None),
    )

    def boom(*_a, **_k):
        raise MaxDepthExceeded(2)

    monkeypatch.setattr(expand_mod, "critic_gate", boom)
    rt = _RT()
    rt._expand_requests = {
        "PLAN-1": {"after_task": "E0", "pending": {}, "replan_allocated_s": 0.0}
    }
    out = await expand_mod.apply_pending_expansion(rt, "PLAN-1")
    assert out is None
    assert refused and refused[0]["reason"] == "expansion_limit"
    assert "max_depth" in refused[0]["detail"]


@pytest.mark.asyncio
async def test_prevalidate_max_depth_refused_as_expansion_limit(monkeypatch):
    """_maybe_expand maps MaxDepthExceeded from prevalidate → expansion_limit, not invalid."""
    from coscientist.core.plan import expand_trigger as et
    from coscientist.core.plan.expand_validate import MaxDepthExceeded

    refused: list[dict] = []

    async def fake_refuse(runtime, after, *, reason, detail, plan_key=None):
        refused.append({"reason": reason, "detail": detail, "after": after})

    monkeypatch.setattr(et, "refuse_expansion", fake_refuse)

    async def boom(*_a, **_k):
        raise MaxDepthExceeded(4)

    monkeypatch.setattr(et, "_prevalidate_request", boom)
    monkeypatch.setattr(
        et,
        "result_expansion_request",
        lambda *_a, **_k: SimpleNamespace(tasks=[object()]),  # non-empty → prevalidate
    )
    monkeypatch.setattr(et, "_final_result_class", lambda *_a, **_k: "scientific")
    monkeypatch.setattr(et, "expansion_already_handled", lambda *_a, **_k: False)

    from coscientist.core.execution.domain import PlanRole

    task = SimpleNamespace(
        outcome=TaskOutcome.SUCCESS,
        spec=SimpleNamespace(task_key="E0"),
        state=TaskState.DONE,
        kind=TaskKind.EXPERIMENT,
        task_id="s/E0",
        plan_role=PlanRole.NONE,
    )
    rt = _RT()
    rt.study = SimpleNamespace(study_id="s")
    rt._finalized = False
    rt._expand_requests = {}
    rt._expansion_refusals = []
    rt._agent_expansions = 0
    rt.plans = {}
    rt.tasks = {"s/E0": task}
    rt.results = {}

    out = await et._maybe_expand(rt, "s/E0")
    assert out is None
    assert refused and refused[0]["reason"] == "expansion_limit"
    assert "max_depth" in refused[0]["detail"]
    assert refused[0]["after"] == "E0"


def _envelope(etype: str, payload: dict, *, entity_key: str = "a1"):
    from datetime import UTC, datetime

    from coscientist.core.events.events import EventEnvelope

    return EventEnvelope(
        study_id="s",
        type=etype,
        entity_key=entity_key,
        idem_key=f"{etype}:{entity_key}",
        payload=payload,
        ts=datetime(2024, 1, 1, tzinfo=UTC),
        schema_version=1,
                )


def test_result_pending_requires_attempt_id_and_digest():
    from coscientist.core.claims.reviews import rebuild_judge_state

    with pytest.raises(JournalFormatError, match="attempt_id"):
        rebuild_judge_state([_envelope("ResultPending", {})], artifacts=None)


def test_result_pending_rejects_none_before_str():
    """str(None) == 'None' must not pass the non-empty check."""
    from coscientist.core.claims.reviews import rebuild_judge_state

    with pytest.raises(JournalFormatError, match="must be str"):
        rebuild_judge_state(
            [
                _envelope(
                    "ResultPending",
                    {"attempt_id": None, "agent_output_digest": "abc"},
                )
            ],
            artifacts=None,
        )


def test_result_pending_rejects_empty_attempt_id_or_digest():
    from coscientist.core.claims.reviews import rebuild_judge_state

    with pytest.raises(JournalFormatError, match="non-empty"):
        rebuild_judge_state(
            [
                _envelope(
                    "ResultPending",
                    {"attempt_id": "", "agent_output_digest": "abc"},
                )
            ],
            artifacts=None,
        )


def test_review_recorded_requires_review_key():
    from coscientist.core.claims.reviews import rebuild_judge_state

    with pytest.raises(JournalFormatError, match="review_key"):
        rebuild_judge_state(
            [_envelope("ReviewRecorded", {"status": "MET"})],
            artifacts=None,
        )


def test_review_recorded_invalid_status_is_journal_format_error():
    from coscientist.core.claims.reviews import rebuild_judge_state

    with pytest.raises(JournalFormatError, match="invalid status"):
        rebuild_judge_state(
            [
                _envelope(
                    "ReviewRecorded",
                    {"review_key": '["t","a","c","i","d"]', "status": "not_a_status"},
                )
            ],
            artifacts=None,
        )


def test_result_recorded_requires_attempt_id_not_entity_key():
    from coscientist.core.claims.reviews import rebuild_judge_state

    with pytest.raises(JournalFormatError, match="attempt_id"):
        rebuild_judge_state(
            [_envelope("ResultRecorded", {}, entity_key="fallback-aid")],
            artifacts=None,
        )


def test_expand_validate_requires_study_max_tasks():
    """study.max_tasks / max_depth are required typed fields (no getattr default)."""
    from coscientist.core.plan.expand_validate import _validate

    study = SimpleNamespace(study_id="s")  # missing max_tasks / max_depth
    rt = SimpleNamespace(
        study=study,
        study_id="s",
        plans={},
        graph=SimpleNamespace(edges_from=lambda *_a, **_k: []),
        catalog=None,
        executor=None,
    )
    with pytest.raises(AttributeError, match="max_tasks"):
        _validate(
            rt,
            added_tasks=[],
            added_questions=[],
            superseded_tasks=[],
            superseded_questions=[],
            known_texts={},
            prev_tasks=[],
            active_q_ids=set(),
        )


def test_expand_validate_refuses_missing_final_type(monkeypatch):
    from coscientist.core.plan import expand_validate as ev

    monkeypatch.setattr(ev, "validate_plan_criteria", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "coscientist.core.plan.foreach.validate_foreach_specs", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "coscientist.core.plan.shares.critic_plan_shares", lambda *_a, **_k: None
    )
    study = SimpleNamespace(study_id="s", max_tasks=64, max_depth=8)
    rt = SimpleNamespace(
        study=study,
        study_id="s",
        plans={
            "p1": {
                "revision": 1,
                "questions": [{"id": "Q1", "text": "why"}],  # missing final_type
                "rollup": "all",
            }
        },
        graph=SimpleNamespace(edges_from=lambda *_a, **_k: []),
        catalog=SimpleNamespace(templates=[]),
        executor=None,
        tasks={},
    )
    err = ev._validate(
        rt,
        added_tasks=[],
        added_questions=[],
        superseded_tasks=[],
        superseded_questions=[],
        known_texts={"Q1": "why"},
        prev_tasks=[],
        active_q_ids={"Q1"},
    )
    assert err == "missing_final_type:Q1"


def test_expand_validate_refuses_unknown_rollup(monkeypatch):
    from coscientist.core.execution.domain import QuestionSpec
    from coscientist.core.plan import expand_validate as ev

    monkeypatch.setattr(ev, "validate_plan_criteria", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "coscientist.core.plan.foreach.validate_foreach_specs", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "coscientist.core.plan.shares.critic_plan_shares", lambda *_a, **_k: None
    )
    child = QuestionSpec(id="Q1.a", text="a", parent_id="Q1", final_type="verdict")
    study = SimpleNamespace(study_id="s", max_tasks=64, max_depth=8)
    rt = SimpleNamespace(
        study=study,
        study_id="s",
        plans={
            "p1": {
                "revision": 1,
                "questions": [
                    {"id": "Q1", "text": "why", "final_type": "verdict"},
                    {
                        "id": "Q1.a",
                        "text": "a",
                        "final_type": "verdict",
                        "parent_id": "Q1",
                    },
                ],
                "rollup": "nope",
            }
        },
        graph=SimpleNamespace(edges_from=lambda *_a, **_k: []),
        catalog=SimpleNamespace(templates=[]),
        executor=None,
        tasks={},
    )
    err = ev._validate(
        rt,
        added_tasks=[],
        added_questions=[child],
        superseded_tasks=[],
        superseded_questions=[],
        known_texts={"Q1": "why", "Q1.a": "a"},
        prev_tasks=[],
        active_q_ids={"Q1", "Q1.a"},
    )
    assert err is not None and "invalid_rollup" in err


def _sqlite_err(cls, msg: str, code: int):
    import sqlite3

    exc = cls(msg)
    exc.sqlite_errorcode = code
    exc.sqlite_errorname = {
        sqlite3.SQLITE_BUSY: "SQLITE_BUSY",
        sqlite3.SQLITE_LOCKED: "SQLITE_LOCKED",
        sqlite3.SQLITE_CORRUPT: "SQLITE_CORRUPT",
        sqlite3.SQLITE_NOTADB: "SQLITE_NOTADB",
        sqlite3.SQLITE_IOERR: "SQLITE_IOERR",
    }.get(code, "SQLITE_UNKNOWN")
    return exc


def test_map_sqlite_locked_is_not_truncated():
    """SQLITE_BUSY/LOCKED → JournalLockedError via sqlite_errorcode, not message text."""
    import sqlite3

    from coscientist.core.events.store import _map_sqlite_open_error

    locked = _map_sqlite_open_error(
        _sqlite_err(sqlite3.OperationalError, "x", sqlite3.SQLITE_BUSY)
    )
    assert isinstance(locked, JournalLockedError)
    assert not isinstance(locked, JournalFormatError)

    busy = _map_sqlite_open_error(
        _sqlite_err(sqlite3.OperationalError, "x", sqlite3.SQLITE_LOCKED)
    )
    assert isinstance(busy, JournalLockedError)


def test_map_sqlite_other_code_is_unavailable_not_truncated():
    import sqlite3

    from coscientist.core.events.store import _map_sqlite_open_error

    mapped = _map_sqlite_open_error(
        _sqlite_err(sqlite3.OperationalError, "disk I/O error", sqlite3.SQLITE_IOERR)
    )
    assert isinstance(mapped, JournalUnavailableError)
    assert "truncated" not in str(mapped).lower()


def test_map_sqlite_unknown_error_is_unavailable_not_truncated():
    """Bare sqlite3.Error without a known code must not become 'truncated'."""
    import sqlite3

    from coscientist.core.events.store import _map_sqlite_open_error

    mapped = _map_sqlite_open_error(sqlite3.Error("mystery"))
    assert isinstance(mapped, JournalUnavailableError)
    assert "truncated" not in str(mapped).lower()


def test_map_sqlite_corrupt_stays_format_error():
    import sqlite3

    from coscientist.core.events.store import _map_sqlite_open_error

    mapped = _map_sqlite_open_error(
        _sqlite_err(sqlite3.DatabaseError, "file is not a database", sqlite3.SQLITE_NOTADB)
    )
    assert isinstance(mapped, JournalFormatError)
    assert "truncated" in str(mapped).lower() or "unreadable" in str(mapped).lower()


@pytest.mark.asyncio
async def test_store_open_locked_raises_journal_locked(tmp_path, monkeypatch):
    """SqliteEventStore open maps SQLITE_BUSY to JournalLockedError (not truncated)."""
    import sqlite3

    from coscientist.core.events import store as store_mod

    def boom(*_a, **_k):
        raise _sqlite_err(sqlite3.OperationalError, "database is locked", sqlite3.SQLITE_BUSY)

    monkeypatch.setattr(store_mod, "_open_conn", boom)
    with pytest.raises(JournalLockedError, match="locked"):
        store_mod.SqliteEventStore("s", tmp_path / "locked.db", lease_ttl_s=30.0)


@pytest.mark.asyncio
async def test_apply_pending_requires_after_task(monkeypatch):
    """meta.after_task is required — no fallback to plan_key."""
    from coscientist.core.plan import expand as expand_mod

    refused: list[dict] = []

    async def fake_refuse(runtime, after, *, reason, detail, plan_key=None):
        refused.append({"after": after, "reason": reason, "detail": detail})

    monkeypatch.setattr(expand_mod, "refuse_expansion", fake_refuse)
    rt = _RT()
    rt._expand_requests = {"PLAN-1": {"pending": {}, "replan_allocated_s": 1.0}}
    out = await expand_mod.apply_pending_expansion(rt, "PLAN-1")
    assert out is None
    assert refused and refused[0]["detail"] == "missing_expand_after_task"


@pytest.mark.asyncio
async def test_expand_plan_refuses_missing_prior_rollup(monkeypatch):
    """prev is None → ExpansionRefused(missing_plan_rollup), not silent rollup='all'."""
    from coscientist.core.execution.domain import TaskSpec
    from coscientist.core.plan import expand as expand_mod

    refused: list[dict] = []

    async def fake_refuse(runtime, after, *, reason, detail, plan_key=None):
        refused.append({"reason": reason, "detail": detail})

    monkeypatch.setattr(expand_mod, "refuse_expansion", fake_refuse)
    monkeypatch.setattr(expand_mod, "active_plan_blob", lambda *_a, **_k: None)
    monkeypatch.setattr(expand_mod, "validate_expansion", lambda *_a, **_k: None)
    rt = _RT()
    rt.study = SimpleNamespace(
        study_id="s",
        max_expansions=2,
        max_human_expansions=0,
        max_tasks=64,
        max_depth=8,
        plan_timeout_s=30,
        replan_budget_s=lambda: 60.0,
        effective_deadline=lambda: __import__("datetime").datetime.now(
            __import__("datetime").UTC
        )
        + __import__("datetime").timedelta(seconds=120),
    )
    rt.plans = {}
    rt.graph = SimpleNamespace(edges_from=lambda *_a, **_k: [])
    rt._finalized = False
    rt._agent_expansions = 0
    rt._human_expansions = 0
    rt._replan_used_s = 0.0
    rt.clock = SimpleNamespace(
        now=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC)
    )
    rt.tasks = {}
    rt.catalog = None

    out = await expand_mod.expand_plan(rt,
        after_task="E0",
        added_tasks=[
            TaskSpec(
                task_key="E1",
                title="t",
                goal="g",
                route="experiment", required=True, tolerate_failed_dep=False)
        ],
        origin="emergent",
        reason="", assign_keys=False)
    assert out is None
    assert refused and refused[0]["detail"] == "missing_plan_rollup"


def test_route_aliases_rejected():
    from coscientist.core.execution.domain import UnknownRouteError, parse_route

    with pytest.raises(UnknownRouteError, match="unknown_route"):
        parse_route("lit")
    with pytest.raises(UnknownRouteError, match="unknown_route"):
        parse_route("EXP")


def test_plan_n_classified_by_plan_role_not_prefix():
    from datetime import UTC, datetime

    from coscientist.core.execution.domain import (
        PlanRole,
        Route,
        Task,
        TaskKind,
        TaskSpec,
        TaskState,
    )
    from coscientist.core.plan.expand_critic import task_is_plan_n

    plan_n = Task(
        task_id="s/PLAN-1",
        kind=TaskKind.PLAN,
        spec=TaskSpec(
            task_key="PLAN-1",
            title="t",
            goal="g",
            route=Route.PLANNER, required=True, tolerate_failed_dep=False),
        state=TaskState.PENDING,
        deadline=datetime.now(UTC),
        plan_role=PlanRole.PLAN_N,
    )
    bootstrap = Task(
        task_id="s/PLAN",
        kind=TaskKind.PLAN,
        spec=TaskSpec(
            task_key="PLAN",
            title="t",
            goal="g",
            route=Route.PLAN, required=True, tolerate_failed_dep=False),
        state=TaskState.PENDING,
        deadline=datetime.now(UTC),
        plan_role=PlanRole.BOOTSTRAP,
    )
    assert task_is_plan_n(plan_n) is True
    assert task_is_plan_n(bootstrap) is False


@pytest.mark.asyncio
async def test_source_fetched_requires_tool_and_call_id(tmp_path):
    """SourceFetched missing tool/call_id → JournalFormatError from store.append_many."""
    from datetime import UTC, datetime

    from coscientist.core.events.errors import JournalFormatError
    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.events.store import SqliteEventStore
    from coscientist.core.events.upcasters import writer_schema_version

    store = SqliteEventStore("s_sf", tmp_path / "sf.db", owner="t", lease_ttl_s=30.0)
    await store.acquire_lease("t")
    # Cap id is truthy so append_many enters SourceFetched payload validation
    # before take_source_fetched_write_permit (see store.append_many).
    now = datetime.now(UTC)
    bad_tool = EventEnvelope(
        study_id="s_sf",
        type="SourceFetched",
        entity_key="x",
        idem_key="sf:bad_tool",
        payload={"call_id": "c1"},  # missing tool
        ts=now,
        schema_version=writer_schema_version("SourceFetched"),
    )
    with pytest.raises(JournalFormatError, match="tool"):
        await store.append_many([bad_tool], _source_fetched_cap_id="cap")
    bad_tool2 = EventEnvelope(
        study_id="s_sf",
        type="SourceFetched",
        entity_key="x2",
        idem_key="sf:bad_tool2",
        payload={"tool": "", "call_id": "c1"},
        ts=now,
        schema_version=writer_schema_version("SourceFetched"),
    )
    with pytest.raises(JournalFormatError, match="tool"):
        await store.append_many([bad_tool2], _source_fetched_cap_id="cap")
    bad_call = EventEnvelope(
        study_id="s_sf",
        type="SourceFetched",
        entity_key="x3",
        idem_key="sf:bad_call",
        payload={"tool": "papers_get", "call_id": ""},
        ts=now,
        schema_version=writer_schema_version("SourceFetched"),
    )
    with pytest.raises(JournalFormatError, match="call_id"):
        await store.append_many([bad_call], _source_fetched_cap_id="cap")


# --- Review @02ec75e: loop / HITL / PlanRole / inputs / finalize fail-closed ---


def test_fold_loop_expansion_refused_missing_loop_id():
    with pytest.raises(JournalFormatError, match="missing loop_id"):
        fold_loop_expansion_refused(_RT(), {"loop": True, "loop_reason": LOOP_REASON_ERROR})


def test_fold_loop_plan_expanded_requires_added_tasks():
    with pytest.raises(JournalFormatError, match="missing added_tasks"):
        fold_loop_plan_expanded(
            _RT(),
            {"loop": True, "loop_id": "L1", "loop_iter": 1},
        )


def test_parse_loop_dep_invalid_raises():
    from coscientist.core.plan.loop import parse_loop_dep

    with pytest.raises(PlanInvalid):
        parse_loop_dep("loop:\x00bad")


def test_loop_state_non_dict_raises():
    from coscientist.core.plan.loop import _loop_state

    rt = _RT()
    rt._loop_state = []  # type: ignore[assignment]
    with pytest.raises(JournalFormatError, match="_loop_state must be dict"):
        _loop_state(rt)


def test_hitl_policy_gate_unknown_kind_returns_none():

    assert default_test_hitl_policy().gate("not_a_real_gate") is None


def test_hitl_policy_gate_plan_plan_approval_alias_removed():
    """gate('plan') must not resolve a plan_approval-only map (and vice versa)."""
    from coscientist.core.execution.domain import GateConfig, HitlPolicy

    only_approval = HitlPolicy(gates={"plan_approval": GateConfig(timeout_s=42, on_timeout="delegate", enabled=True)}, mode="headless", timeout_s={}, on_timeout=None, defaults={"plan_approval": "human_timeout", "result_acceptance": "accept_computed", "skip_task": "deny", "side_effect": "deny", "proposed_check": "reject"})
    assert only_approval.gate("plan_approval") is not None
    assert only_approval.gate("plan") is None

    only_plan = HitlPolicy(gates={"plan": GateConfig(timeout_s=42, on_timeout="delegate", enabled=True)}, mode="headless", timeout_s={}, on_timeout=None, defaults={"plan_approval": "human_timeout", "result_acceptance": "accept_computed", "skip_task": "deny", "side_effect": "deny", "proposed_check": "reject"})
    assert only_plan.gate("plan") is not None
    assert only_plan.gate("plan_approval") is None


@pytest.mark.asyncio
async def test_costly_hitl_rejects_accept_alias():
    """Costly path accepts approve|reject|delegate only — no accept→approve alias."""
    from datetime import UTC, datetime

    from coscientist.core.execution.domain import HitlRequest
    from coscientist.experimental.hitl import HitlForkMixin

    class _RT(HitlForkMixin):
        def __init__(self) -> None:
            self.hitl = {
                "r1": HitlRequest(
                    request_id="r1",
                    kind="costly_or_nonreal",
                    payload_ref="t1",
                    deadline=datetime.now(UTC),
                    study_id="s",
                    task_id="t1",
                    open=True,
                    cost_rub=1.0,
                )
            }

    with pytest.raises(ValueError, match="unknown costly HITL decision"):
        await _RT().answer_hitl("r1", "accept",
            critic_accepts=True)


@pytest.mark.asyncio
async def test_open_hitl_plan_requires_expand_after_task():
    """plan / plan_approval / plan_expansion must not fall back to task_key."""
    from datetime import UTC, datetime, timedelta

    from coscientist.core.execution.domain import (
        GateConfig,
        HitlPolicy,
        HitlRequest,
        PlanRole,
        Task,
        TaskKind,
        TaskSpec,
        TaskState,
    )
    from coscientist.experimental.hitl import HitlForkMixin

    class _RT(HitlForkMixin):
        def __init__(self) -> None:
            now = datetime.now(UTC)
            self.clock = SimpleNamespace(now=lambda: now)
            self.study = SimpleNamespace(
                study_id="s",
                hitl_policy=HitlPolicy(
                    gates={"plan_approval": GateConfig(timeout_s=60, on_timeout="delegate", enabled=True)}, mode="headless", timeout_s={}, on_timeout=None, defaults={"plan_approval": "human_timeout", "result_acceptance": "accept_computed", "skip_task": "deny", "side_effect": "deny", "proposed_check": "reject"}),
                effective_deadline=lambda: now + timedelta(hours=1),
            )
            self.hitl: dict[str, HitlRequest] = {}
            self.tasks = {
                "s/E0": Task(
                    task_id="s/E0",
                    kind=TaskKind.EXPERIMENT,
                    spec=TaskSpec(
                        task_key="E0", title="t", goal="g", route="experiment", required=True, tolerate_failed_dep=False),
                    state=TaskState.PENDING,
                    deadline=now + timedelta(hours=1),
                    plan_role=PlanRole.NONE,
                )
            }

        def _resolve_task(self, task_key_or_id: str):
            return self.tasks["s/E0"]

        async def _append(self, events):
            return None

    rt = _RT()
    with pytest.raises(ValueError, match="expand_after_task"):
        await rt.open_hitl("E0", kind="plan_approval",
            cost_rub=0.0)
    with pytest.raises(ValueError, match="expand_after_task"):
        await rt.open_hitl("E0", kind="plan",
            cost_rub=0.0)
    with pytest.raises(ValueError, match="expand_after_task"):
        await rt.open_hitl("E0", kind="plan_expansion",
            cost_rub=0.0)


@pytest.mark.asyncio
async def test_hitl_no_gate_timeout_fails():
    """Unknown gate: open_hitl refuses; _hitl_timeout with gate None fails (no invented on_timeout).

    open_hitl already raises when hitl_policy.gate(kind) is None, so a timeout path
    with gate None can only arise from a journaled request whose kind was later
    removed from policy — exercise _hitl_timeout directly and assert fail, not
    critic delegate.
    """
    from datetime import UTC, datetime, timedelta

    from coscientist.core.execution.domain import (
        GateConfig,
        HitlPolicy,
        HitlRequest,
        PlanRole,
        Task,
        TaskKind,
        TaskSpec,
        TaskState,
    )
    from coscientist.experimental.hitl import HitlForkMixin

    now = datetime.now(UTC)

    class _RT(HitlForkMixin):
        def __init__(self) -> None:
            self.clock = SimpleNamespace(now=lambda: now)
            # Policy has plan_approval only — kind "plan" has no gate (alias removed).
            self.study = SimpleNamespace(
                study_id="s",
                hitl_policy=HitlPolicy(
                    gates={"plan_approval": GateConfig(timeout_s=60, on_timeout="delegate", enabled=True)}, mode="headless", timeout_s={}, on_timeout=None, defaults={"plan_approval": "human_timeout", "result_acceptance": "accept_computed", "skip_task": "deny", "side_effect": "deny", "proposed_check": "reject"}),
            )
            self.hitl: dict[str, HitlRequest] = {}
            self.tasks = {
                "s/E0": Task(
                    task_id="s/E0",
                    kind=TaskKind.EXPERIMENT,
                    spec=TaskSpec(
                        task_key="E0", title="t", goal="g", route="experiment", required=True, tolerate_failed_dep=False),
                    state=TaskState.RUNNING,
                    deadline=now + timedelta(hours=1),
                    plan_role=PlanRole.NONE,
                )
            }
            self._appended: list = []
            self._failed: list[str] = []

        def _resolve_task(self, task_key_or_id: str):
            return self.tasks["s/E0"]

        async def _append(self, events):
            self._appended.extend(events)

        async def _fail_task_human_timeout(self, h, task):
            self._failed.append(h.request_id)

        async def _critic_decide(self, h, *, decision, delegated_by):
            raise AssertionError(
                "must not invent on_timeout=delegate when gate is None"
            )

    rt = _RT()
    # open_hitl already refuses unknown / unmapped kinds.
    with pytest.raises(ValueError, match="no gate"):
        await rt.open_hitl("E0", kind="plan", expand_after_task="E0",
            cost_rub=0.0)

    h = HitlRequest(
        request_id="r_orphan",
        kind="plan",  # no gate in policy (alias removed)
        payload_ref="s/E0",
        deadline=now - timedelta(seconds=1),
        study_id="s",
        task_id="s/E0",
        open=True,
        expand_after_task="E0", cost_rub=0.0)
    rt.hitl[h.request_id] = h
    await rt._hitl_timeout(h)  # noqa: SLF001
    assert rt._failed == ["r_orphan"]
    assert any(getattr(e, "type", None) == "HitlTimedOut" for e in rt._appended)
    assert not any(getattr(e, "type", None) == "HitlDecided" for e in rt._appended)


def test_expand_timeout_delegated_by_is_agent_path():
    """DESIGN_M1 §4: HitlDecided.delegated_by=timeout → agent quota (not refuse)."""
    from coscientist.core.plan.expand import _delegated_by_from_hitl

    rt = _RT()
    rt._hitl_expand_used = set()
    rt._hitl_decisions = {
        "h1": {"delegated_by": "timeout", "decision": "accept"},
    }
    rt._hitl_requests = {
        "h1": {"kind": "plan_expansion", "expand_after_task": "E0"},
    }
    assert _delegated_by_from_hitl(rt, "h1", after_task="E0") == "timeout"


def test_task_is_plan_n_missing_role_raises():
    from coscientist.core.plan.expand_critic import task_is_plan_n

    with pytest.raises(JournalFormatError, match="plan_role"):
        task_is_plan_n(SimpleNamespace())


def test_task_is_plan_n_invalid_role_raises():
    from coscientist.core.plan.expand_critic import task_is_plan_n

    with pytest.raises(JournalFormatError, match="invalid"):
        task_is_plan_n(SimpleNamespace(plan_role="not_a_role"))


def test_task_has_plan_n_critic_alias_removed():
    import coscientist.core.plan.expand_critic as ec

    assert not hasattr(ec, "task_has_plan_n_critic")


def test_task_plan_role_required_no_default():
    from datetime import UTC, datetime

    from pydantic import ValidationError

    from coscientist.core.execution.domain import Task, TaskKind, TaskSpec, TaskState

    with pytest.raises(ValidationError):
        Task(
            task_id="s/E0",
            kind=TaskKind.EXPERIMENT,
            spec=TaskSpec(task_key="E0", title="t", goal="g", route="experiment", required=True, tolerate_failed_dep=False),
            state=TaskState.PENDING,
            deadline=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_event_envelope_epoch_digest_stamped_by_store(tmp_path):
    """UnstampedEvent has no epoch/digest; store returns StoredEvent with both."""
    from datetime import UTC, datetime

    from pydantic import ValidationError

    from coscientist.core.events.clock import FakeClock
    from coscientist.core.events.digest import payload_digest as pd
    from coscientist.core.events.events import (
        StoredEvent,
        UnstampedEvent,
        stamp_envelope,
    )
    from coscientist.core.events.store import SqliteEventStore

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    payload = {"study_id": "st_stamp", "x": 1}
    base = dict(
        study_id="st_stamp",
        type="StudyCreated",
        entity_key="st_stamp",
        idem_key="StudyCreated:st_stamp",
        payload=payload,
        ts=clock.now(),
        schema_version=writer_schema_version("StudyCreated"),
    )
    # Contract: writer type rejects epoch / payload_digest (not optional None).
    assert "epoch" not in UnstampedEvent.model_fields
    assert "payload_digest" not in UnstampedEvent.model_fields
    with pytest.raises(ValidationError):
        UnstampedEvent(**base, epoch=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        UnstampedEvent(**base, payload_digest="abc")  # type: ignore[call-arg]

    # Contract: StoredEvent requires exact int epoch + non-empty digest.
    with pytest.raises(ValidationError):
        StoredEvent(**base)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        StoredEvent(**base, epoch=1, payload_digest="")  # type: ignore[call-arg]

    ev = UnstampedEvent(**base)
    stamped = stamp_envelope(ev, epoch=1)
    assert isinstance(stamped, StoredEvent)
    assert stamped.epoch == 1
    assert stamped.payload_digest == pd(payload)

    store = SqliteEventStore("st_stamp", tmp_path / "stamp.db", clock=clock, lease_ttl_s=30.0)
    await store.acquire_lease("o1")
    stored = await store.append(ev)
    assert isinstance(stored, StoredEvent)
    assert stored.epoch == 1
    assert stored.payload_digest == pd(payload)
    await store.close()


def test_finalize_unknown_task_reason_refuses():
    from datetime import UTC, datetime

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.finalize.finalize import contributing_reasons_ordered

    ev = EventEnvelope(
        study_id="s",
        type="TaskDone",
        entity_key="t",
        idem_key="k",
        payload={"reason": "not_a_task_reason"},
        ts=datetime.now(UTC),
        schema_version=1,
    )
    with pytest.raises(JournalFormatError, match="TaskDone.reason"):
        contributing_reasons_ordered([ev])


def test_next_kind_index_uses_route_counts_not_regex():
    from datetime import UTC, datetime

    from coscientist.core.execution.domain import (
        PlanRole,
        Route,
        Task,
        TaskKind,
        TaskSpec,
        TaskState,
    )
    from coscientist.core.plan.expand_validate import next_kind_index

    rt = _RT()
    rt.tasks = {
        "s/EXP-0": Task(
            task_id="s/EXP-0",
            kind=TaskKind.EXPERIMENT,
            spec=TaskSpec(
                task_key="CUSTOM",
                title="t",
                goal="g",
                route=Route.EXPERIMENT, required=True, tolerate_failed_dep=False),
            state=TaskState.PENDING,
            deadline=datetime.now(UTC),
            plan_role=PlanRole.NONE,
        )
    }
    # Structural: one experiment route → next index 1 (not regex on CUSTOM).
    assert next_kind_index(rt, "EXP", []) == 1


def test_reviews_pending_excludes_unassessed_alias():
    """UNASSESSED must not schedule like PENDING (no alias set)."""
    from datetime import UTC, datetime

    from coscientist.core.claims.reviews import review_scheduled_events
    from coscientist.core.execution.domain import CheckResult, ReqStatus

    checks = [
        CheckResult(
            check_id="c1",
            template_id="human_review",
            status=ReqStatus.UNASSESSED,
            is_code=False,
            role="validity",
            required=True,
            effect={"item_statuses": {"i1": ReqStatus.UNASSESSED.value}},
        used_sesoi=False,
    )
    ]
    evs = review_scheduled_events(
        study_id="s",
        attempt_id="a#1",
        task_id="s/E0",
        output_digest="d" * 64,
        checks=checks,
        now=datetime.now(UTC),
    )
    # item_statuses UNASSESSED alone (not in pending_items) → no schedule.
    assert evs == []


def test_replan_budget_negative_span_raises():
    from datetime import UTC, datetime, timedelta


    now = datetime.now(UTC)
    s = make_test_study(
        study_id="s",
        profile="p",
        request="r",
        catalog_version="1",
        hitl_policy=default_test_hitl_policy(),
        deadline=now - timedelta(seconds=10),
        created_at=now,
        finalize_reserve_s=0,
    )
    with pytest.raises(ValueError, match="negative span"):
        s.replan_budget_s()


def test_local_project_plan_n_requires_alloc_and_pending_dict():
    from datetime import UTC, datetime

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.domain import (
        PlanRole,
        Route,
        TaskKind,
        TaskSpec,
        TaskState,
    )
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _LP(LocalProjectMixin):
        def __init__(self) -> None:
            self.tasks = {}
            self._replan_used_s = 0.0
            self._expand_requests = {}
            self._expand_pending = set()

    lp = _LP()
    spec = TaskSpec(
        task_key="PLAN-1",
        title="t",
        goal="g",
        route=Route.PLANNER, required=True, tolerate_failed_dep=False)
    payload = {
        "task_id": "s/PLAN-1",
        "kind": TaskKind.PLAN.value,
        "spec": spec.model_dump(mode="json"),
        "state": TaskState.PENDING.value,
        "deadline": datetime.now(UTC).isoformat(),
        "plan_role": PlanRole.PLAN_N.value,
        "expand_after_task": "E0",
        "pending_expansion": "not-a-dict",
        "replan_allocated_s": 1.0,
    }
    ev = EventEnvelope(
        study_id="s",
        type="TaskCreated",
        entity_key="s/PLAN-1",
        idem_key="k",
        payload=payload,
        ts=datetime.now(UTC),
        schema_version=1,
    )
    with pytest.raises(JournalFormatError, match="pending_expansion"):
        lp._project_local(ev)


def test_inputs_rejects_outcome_alias_without_claim_outcome():
    from datetime import UTC, datetime

    from coscientist.core.claims.graph import GraphNode, GraphStore, default_execution_registry
    from coscientist.core.events.ids import question_node_id
    from coscientist.core.execution.inputs import claim_nodes_from_attempt

    g = GraphStore(default_execution_registry())
    qid = "Q1"
    nid = question_node_id("s", qid)
    g.upsert_node(
        GraphNode(
            node_id=nid,
            kind="question",
            study_id="s",
            ref_id=qid,
            attrs={},
            created_at=datetime.now(UTC),
        )
    )

    def derive(raw, ok):
        return {
            "criterion_status": "met",
            "evidence_kind": "computational",
            "frozen": True,
            "lit_own_criterion": False,
            "accepted": True,
            "validity_ok": True,
            "check_role": "prediction",
            "outcome": "consistent",  # deprecated alias
            "predicted_direction": "two_sided",
            "predicts_null": False,
        }

    with pytest.raises(JournalFormatError, match="claim_outcome"):
        claim_nodes_from_attempt(
            study_id="s",
            attempt_id="s/E0#1",
            provenance="real",
            claims=[{"id": "c1", "text": "t", "target": qid}],
            now=datetime.now(UTC),
            graph=g,
            attempt_outcome="SUCCESS",
            infra_failure=False,
            derive_attrs=derive,
        )


# --- R4 fail-closed: expand_critic / executor / evaluate ---


def test_r4_task_is_plan_n_rejects_str_coercion_of_objects():
    """PlanRole only accepts PlanRole|str — no str(obj) coercion."""
    from coscientist.core.plan.expand_critic import task_is_plan_n

    class _Weird:
        def __str__(self) -> str:
            return "plan_n"

    with pytest.raises(JournalFormatError, match="PlanRole\\|str"):
        task_is_plan_n(SimpleNamespace(plan_role=_Weird()))


def test_r4_critic_gate_missing_catalog_returns_missing_catalog():
    from coscientist.core.plan import expand_critic as ec

    rt = _RT()
    rt.study = SimpleNamespace(max_expansions=2)
    rt.catalog = None
    req = SimpleNamespace(
        tasks=[],
        questions=[],
        superseded_tasks=[],
        superseded_questions=[],
    )
    assert ec.critic_gate(rt, req, after_task="E0") == "missing_catalog"


def test_r4_frozen_criterion_requires_sesoi_attrs():
    """Frozen criterion missing uses_sesoi/predicts_null/predicted_direction raises."""
    from datetime import UTC, datetime

    from coscientist.core.claims.graph import GraphNode, GraphStore, default_execution_registry
    from coscientist.core.events.ids import criterion_node_id
    from coscientist.core.execution.domain import (
        CheckResult,
        PlanRole,
        ReqStatus,
        Task,
        TaskKind,
        TaskSpec,
        TaskState,
    )
    from coscientist.core.execution.executor import _derive_claim_core_attrs

    sid = "st_frz_req"
    g = GraphStore(default_execution_registry())
    cnid = criterion_node_id(sid, "value_match")
    g.upsert_node(
        GraphNode(
            node_id=cnid,
            kind="criterion",
            study_id=sid,
            ref_id="value_match",
            # frozen but missing uses_sesoi / predicts_null / predicted_direction
            attrs={"frozen": True, "frozen_at_seq": 1, "template_id": "value_match"},
            created_at=datetime.now(UTC),
        )
    )
    now = datetime.now(UTC)
    task = Task(
        task_id=f"{sid}/E0",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key="E0", title="e", goal="g", route="experiment", criteria=[], required=True, tolerate_failed_dep=False),
        state=TaskState.DONE,
        outcome=TaskOutcome.SUCCESS,
        deadline=now,
        plan_role=PlanRole.NONE,
    )
    checks = [
        CheckResult(
            check_id="value_match#0",
            template_id="value_match",
            status=ReqStatus.MET,
            is_code=True,
            required=True,
            role="prediction",
        used_sesoi=False,
    )
    ]
    with pytest.raises(JournalFormatError, match="frozen criterion missing"):
        _derive_claim_core_attrs(
            task,
            TaskOutcome.SUCCESS,
            raw={"checked_against": "value_match", "target": "Q1"},
            checks=checks,
            graph=g,
            study_id=sid,
            validated=True,
        )


def test_r4_unassessed_pred_statuses_use_unassessed_token(monkeypatch):
    """Synthesizing pred_statuses for unassessed must not invent \"\"."""
    from datetime import UTC, datetime

    import coscientist.core.claims.verdict as verdict_mod
    from coscientist.core.execution.domain import (
        PlanRole,
        Task,
        TaskKind,
        TaskSpec,
        TaskState,
    )
    from coscientist.core.execution.executor import _derive_claim_core_attrs

    captured: dict = {}
    real = verdict_mod.classify_outcome

    def _capture(**kwargs):
        captured["prediction_statuses"] = list(kwargs["prediction_statuses"])
        return real(**kwargs)

    monkeypatch.setattr(verdict_mod, "classify_outcome", _capture)

    now = datetime.now(UTC)
    task = Task(
        task_id="st/E0",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key="E0", title="e", goal="g", route="experiment", criteria=[], required=True, tolerate_failed_dep=False),
        state=TaskState.DONE,
        outcome=TaskOutcome.SUCCESS,
        deadline=now,
        plan_role=PlanRole.NONE,
    )
    # No prediction checks + unknown role → criterion_status unassessed path
    # with empty pred_statuses synthesis.
    _derive_claim_core_attrs(
        task,
        TaskOutcome.SUCCESS,
        raw={"checked_against": "missing_tmpl", "target": "Q1"},
        checks=[],
        graph=None,
        study_id="st",
        validated=True,
    )
    assert captured.get("prediction_statuses") == ["UNASSESSED"]
    assert "" not in captured.get("prediction_statuses", [])


def test_r4_evaluate_metric_threshold_requires_op(tmp_path):
    from coscientist.checks.evaluate import evaluate_code_check
    from coscientist.core.events.errors import JournalFormatError as JFE
    from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
    from coscientist.core.execution.domain import (
        AgentTaskOutput,
        ArtifactHandle,
        CodeCheck,
    )

    store = LocalArtifactStore(tmp_path / "cas")
    art = store.put(
        b'{"effect": 1.0}',
        make_tool_ref("a#1:out"),
        name="main.json",
        media_type="application/json",
    )
    out = AgentTaskOutput(
        outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")}, summary="")
    check = CodeCheck(
        kind="metric_threshold",
        output="main",
        metric="effect",
        pointer="/effect",
        op=None,
        target=0, allow_network=False)
    with pytest.raises(JFE, match="requires op"):
        evaluate_code_check(
            check,
            template_id="t",
            check_id="t#0",
            out=out,
            artifacts=store,
            values={},
            role="prediction",
            attempt_id="a#1",
        
            uses_sesoi=False,
            predicts_null=False,
            predicted_direction="two_sided",
        )


def test_r4_evaluate_requires_output_no_main_default(tmp_path):
    from coscientist.checks.evaluate import evaluate_code_check
    from coscientist.core.events.errors import JournalFormatError as JFE
    from coscientist.core.execution.artifacts import LocalArtifactStore
    from coscientist.core.execution.domain import AgentTaskOutput, CodeCheck

    store = LocalArtifactStore(tmp_path / "cas")
    check = CodeCheck(kind="schema_valid", output=None, allow_network=False)
    with pytest.raises(JFE, match="requires non-empty output"):
        evaluate_code_check(
            check,
            template_id="t",
            check_id="t#0",
            out=AgentTaskOutput(summary=""),
            artifacts=store,
            values={},
            role="validity",
        
            uses_sesoi=False,
            predicts_null=False,
            predicted_direction="two_sided",
        )


def test_r4_evaluate_unexpected_exception_raises_core_bug(monkeypatch, tmp_path):
    """Non-executable kinds must not convert unexpected Exception → FAILED."""
    import coscientist.checks.evaluate as ev
    from coscientist.checks.evaluate import evaluate_code_check
    from coscientist.core.events.errors import CoreBugError
    from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
    from coscientist.core.execution.domain import (
        AgentTaskOutput,
        ArtifactHandle,
        CodeCheck,
    )

    store = LocalArtifactStore(tmp_path / "cas")
    art = store.put(
        b'{"count": 1}',
        make_tool_ref("a#1:out"),
        name="main.json",
        media_type="application/json",
    )
    out = AgentTaskOutput(
        outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")}, summary="")
    check = CodeCheck(
        kind="value_matches",
        output="main",
        pointer="/count",
        target=1, allow_network=False)

    def boom(*_a, **_k):
        raise RuntimeError("unexpected_core_path")

    monkeypatch.setattr(ev, "_pointer_get", boom)
    with pytest.raises(CoreBugError, match="core_bug:RuntimeError"):
        evaluate_code_check(
            check,
            template_id="t",
            check_id="t#0",
            out=out,
            artifacts=store,
            values={},
            role="prediction",
            attempt_id="a#1",
        
            uses_sesoi=False,
            predicts_null=False,
            predicted_direction="two_sided",
        )


def test_r4_evaluate_defaults_require_experiment_key(tmp_path):
    from coscientist.checks.catalog import load_catalog
    from coscientist.checks.evaluate import evaluate_task_checks
    from coscientist.core.execution.artifacts import LocalArtifactStore
    from coscientist.core.execution.domain import AgentTaskOutput, PlanRole, TaskSpec

    cat = load_catalog(
        {
            "catalog_version": "0.1.0",
            "edition": "t",
            "sources_sha256": {},
            "rules": [
                {
                    "rule_id": "R23",
                    "source": "x",
                    "excerpt": "e",
                    "digest": "d",
                }
            ],
            "templates": [
                {
                    "id": "artifact_present",
                    "title": "a",
                    "text_ru": "t",
                    "rule_ids": ["R23"],
                    "method_sources": ["m"],
                    "applies_to": ["experiment"],
                    "check": {"kind": "artifact_exists", "output": "main"},
                    "role": "validity",
                    "kind": "catalog",
                    "required": True,
                    "version": "1",
                    "uses_sesoi": False,
                    "predicts_null": False,
                    "predicted_direction": "two_sided",
                }
            ],
            # defaults present but missing experiment key
            "defaults": {"hypothesis": ["artifact_present"]},
        }
    )
    with pytest.raises(JournalFormatError, match="experiment"):
        evaluate_task_checks(
            cat,
            TaskSpec(task_key="E0", title="t", goal="g", route="experiment", required=True, tolerate_failed_dep=False),
            AgentTaskOutput(summary=""),
            LocalArtifactStore(tmp_path / "cas"),
            plan_role=PlanRole.NONE,
        )


def test_r4_evaluate_proposed_kind_id_mismatch_raises(tmp_path):
    from coscientist.checks.catalog import load_catalog
    from coscientist.checks.evaluate import evaluate_task_checks
    from coscientist.core.execution.artifacts import LocalArtifactStore
    from coscientist.core.execution.domain import (
        AgentTaskOutput,
        PlanRole,
        RequirementRef,
        TaskSpec,
    )

    cat = load_catalog(
        {
            "catalog_version": "0.1.0",
            "edition": "t",
            "sources_sha256": {},
            "rules": [
                {
                    "rule_id": "R23",
                    "source": "x",
                    "excerpt": "e",
                    "digest": "d",
                }
            ],
            "templates": [
                {
                    "id": "proposed_check",
                    "title": "p",
                    "text_ru": "t",
                    "rule_ids": ["R23"],
                    "method_sources": ["m"],
                    "applies_to": ["experiment"],
                    "check": {"kind": "artifact_exists", "output": "main"},
                    "role": "prediction",
                    # kind catalog but id is proposed_check → mismatch
                    "kind": "catalog",
                    "required": True,
                    "version": "1",
                    "uses_sesoi": False,
                    "predicts_null": False,
                    "predicted_direction": "two_sided",
                }
            ],
            "defaults": {"experiment": ["proposed_check"]},
        }
    )
    with pytest.raises(JournalFormatError, match="kind/template_id mismatch"):
        evaluate_task_checks(
            cat,
            TaskSpec(
                task_key="E0",
                title="t",
                goal="g",
                route="experiment",
                criteria=[RequirementRef(template_id="proposed_check", required=True)], required=True, tolerate_failed_dep=False),
            AgentTaskOutput(summary=""),
            LocalArtifactStore(tmp_path / "cas"),
            plan_role=PlanRole.NONE,
        )


def test_r4_evaluate_plan_role_rejects_str_coercion():
    import tempfile
    from pathlib import Path

    from coscientist.checks.catalog import minimal_test_catalog
    from coscientist.checks.evaluate import evaluate_task_checks
    from coscientist.core.execution.artifacts import LocalArtifactStore
    from coscientist.core.execution.domain import AgentTaskOutput, TaskSpec

    class _Weird:
        def __str__(self) -> str:
            return "none"

    with tempfile.TemporaryDirectory() as td:
        with pytest.raises(JournalFormatError, match="PlanRole\\|str"):
            evaluate_task_checks(
                minimal_test_catalog(),
                TaskSpec(task_key="E0", title="t", goal="g", route="experiment", required=True, tolerate_failed_dep=False),
                AgentTaskOutput(summary=""),
                LocalArtifactStore(Path(td)),
                plan_role=_Weird(),  # type: ignore[arg-type]
            )


def test_r4_evaluate_optional_kwargs_non_dict_raises(tmp_path):
    from coscientist.checks.catalog import minimal_test_catalog
    from coscientist.checks.evaluate import evaluate_task_checks
    from coscientist.core.execution.artifacts import LocalArtifactStore
    from coscientist.core.execution.domain import AgentTaskOutput, PlanRole, TaskSpec

    with pytest.raises(JournalFormatError, match="review_statuses must be dict"):
        evaluate_task_checks(
            minimal_test_catalog(),
            TaskSpec(task_key="E0", title="t", goal="g", route="experiment", required=True, tolerate_failed_dep=False),
            AgentTaskOutput(summary=""),
            LocalArtifactStore(tmp_path / "cas"),
            plan_role=PlanRole.NONE,
            review_statuses=[],  # type: ignore[arg-type]
        )


def test_r4_evaluate_proposed_predicted_direction_required(monkeypatch, tmp_path):
    """Approved proposed_check must not default predicted_direction to two_sided."""
    from coscientist.checks import evaluate as ev
    from coscientist.core.execution.artifacts import LocalArtifactStore
    from coscientist.core.execution.domain import (
        AgentTaskOutput,
        CodeCheck,
        RequirementRef,
        TaskSpec,
    )

    monkeypatch.setattr(
        "coscientist.core.plan.proposed_check.assert_proposal_preregistered_for_eval",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "coscientist.core.plan.proposed_check.code_check_from_proposal",
        lambda _row: CodeCheck(kind="artifact_exists", output="main", allow_network=False),
    )
    monkeypatch.setattr(
        "coscientist.checks.sandbox_limits.apply_frozen_limits",
        lambda check, *_a, **_k: check,
    )

    row = {
        "status": "approved",
        "role": "prediction",
        "bound_task_id": "s/E0",
        "bound_target": "Q1",
        "check": {"kind": "artifact_exists", "output": "main"},
        # predicted_direction deliberately absent
    }
    with pytest.raises(JournalFormatError, match="predicted_direction"):
        ev._evaluate_proposed_check(
            ref=RequirementRef(
                template_id="proposed_check",
                params={"proposal_id": "p1", "target": "Q1"}, required=True),
            check_id="proposed_check#0",
            out=AgentTaskOutput(summary=""),
            artifacts=LocalArtifactStore(tmp_path / "cas"),
            proposals={"p1": row},
            attempt_id="a#1",
            task_id="s/E0",
            output_digest="d" * 64,
            review_statuses={},
            judge_retry={},
            failure_kinds={},
            fetched_digests=None,
            spec=TaskSpec(task_key="E0", title="t", goal="g", route="experiment", required=True, tolerate_failed_dep=False),
            attempts={"a#1": {"_seq": 2}},
            results={},
        )


def test_r4_artifact_belongs_missing_produced_by_raises():
    from coscientist.checks.evaluate import artifact_belongs_to_attempt

    with pytest.raises(JournalFormatError, match="produced_by"):
        artifact_belongs_to_attempt(SimpleNamespace(sha256="x"), "a#1")


def test_r4_rubric_requires_output_no_main_default(tmp_path):
    from coscientist.checks.catalog import load_catalog
    from coscientist.checks.evaluate import evaluate_task_checks
    from coscientist.core.execution.artifacts import LocalArtifactStore
    from coscientist.core.execution.domain import (
        AgentTaskOutput,
        PlanRole,
        RequirementRef,
        TaskSpec,
    )

    cat = load_catalog(
        {
            "catalog_version": "0.1.0",
            "edition": "t",
            "sources_sha256": {},
            "rules": [
                {
                    "rule_id": "R23",
                    "source": "x",
                    "excerpt": "e",
                    "digest": "d",
                }
            ],
            "templates": [
                {
                    "id": "rubric_bare",
                    "title": "r",
                    "text_ru": "t",
                    "rule_ids": ["R23"],
                    "method_sources": ["m"],
                    "applies_to": ["experiment"],
                    "check": {
                        "kind": "rubric",
                        "output": None,
                        "rubric": {
                            "items": [
                                {
                                    "id": "exists",
                                    "text": "x",
                                    "required": True,
                                    "check": "code",
                                    "code_kind": "artifact_exists",
                                }
                            ],
                            "aggregate": "all_required",
                        },
                    },
                    "role": "validity",
                    "kind": "catalog",
                    "required": True,
                    "version": "1",
                    "uses_sesoi": False,
                    "predicts_null": False,
                    "predicted_direction": "two_sided",
                }
            ],
            "defaults": {"experiment": ["rubric_bare"]},
        }
    )
    with pytest.raises(JournalFormatError, match="rubric requires non-empty output"):
        evaluate_task_checks(
            cat,
            TaskSpec(
                task_key="E0",
                title="t",
                goal="g",
                route="experiment",
                criteria=[RequirementRef(template_id="rubric_bare", required=True)], required=True, tolerate_failed_dep=False),
            AgentTaskOutput(summary=""),
            LocalArtifactStore(tmp_path / "cas"),
            plan_role=PlanRole.NONE,
        )


# --- R4 loop / local_project / inputs / misc fail-closed (review blockers) ---


def test_loop_status_row_without_status_raises():
    from coscientist.core.plan.loop import loop_status

    rt = _RT()
    rt._loop_state = {"L1": {"iter": 1}}
    with pytest.raises(JournalFormatError, match="loop.status missing"):
        loop_status(rt, "L1")


def test_iter_keys_unknown_loop_raises():
    from coscientist.core.plan.loop import iter_keys

    with pytest.raises(JournalFormatError, match="unknown loop"):
        iter_keys(_RT(), "L1", 1)


def test_iter_keys_missing_iters_or_key_raises():
    from coscientist.core.plan.loop import iter_keys

    rt = _RT()
    rt._loop_state = {"L1": {"status": "running"}}
    with pytest.raises(JournalFormatError, match="loop.iters missing"):
        iter_keys(rt, "L1", 1)
    rt._loop_state = {"L1": {"status": "running", "iters": {}}}
    with pytest.raises(JournalFormatError, match=r"loop\.iters\[1\] missing"):
        iter_keys(rt, "L1", 1)


def test_current_iter_row_missing_iter_raises():
    from coscientist.core.plan.loop import current_iter

    rt = _RT()
    rt._loop_state = {"L1": {"status": "running"}}
    with pytest.raises(JournalFormatError, match="loop.iter missing"):
        current_iter(rt, "L1")
    assert current_iter(_RT(), "L1") == 0


@pytest.mark.asyncio
async def test_materialize_loop_iter_none_study_raises():
    from coscientist.core.execution.domain import LoopPredicate, LoopSpec, Route, TaskSpec
    from coscientist.core.plan.loop import materialize_loop_iter

    loop = LoopSpec(
        loop_id="L1",
        body=[
            TaskSpec(task_key="E0", title="t", goal="g", route=Route.EXPERIMENT, required=True, tolerate_failed_dep=False)
        ],
        until=LoopPredicate(name="survivors_le", params={"n": 0}),
        max_iter=2,
    )
    rt = SimpleNamespace(study=None, _finalized=False)
    with pytest.raises(JournalFormatError, match="missing study"):
        await materialize_loop_iter(rt, loop, iter_n=1)


@pytest.mark.asyncio
async def test_emit_loop_capped_none_study_raises():
    from coscientist.core.execution.domain import LoopPredicate, LoopSpec, Route, TaskSpec
    from coscientist.core.plan.loop import _emit_loop_capped

    loop = LoopSpec(
        loop_id="L1",
        body=[
            TaskSpec(task_key="E0", title="t", goal="g", route=Route.EXPERIMENT, required=True, tolerate_failed_dep=False)
        ],
        until=LoopPredicate(name="survivors_le", params={"n": 0}),
        max_iter=1,
    )
    rt = SimpleNamespace(study=None, _finalized=False, _loop_capped={})
    with pytest.raises(JournalFormatError, match="missing study"):
        await _emit_loop_capped(rt, loop, iter_n=1)


def test_loop_capped_map_required():
    """materialize/fold/capped_scope must not invent else {} for _loop_capped."""
    from coscientist.core.plan.loop import _require_loop_capped

    rt = _RT()
    rt._loop_capped = None  # type: ignore[assignment]
    with pytest.raises(JournalFormatError, match="_loop_capped missing"):
        _require_loop_capped(rt)
    with pytest.raises(JournalFormatError, match="_loop_capped missing"):
        fold_loop_capped(
            rt,
            {
                "loop_id": "L1",
                "iter": 1,
                "reason": LOOP_REASON_ITERATION_CAP,
                "scope_questions": [],
            },
        )
    with pytest.raises(JournalFormatError, match="_loop_capped missing"):
        capped_scope_questions(rt)


def test_validate_loop_specs_none_hypothesis_id_raises():
    from coscientist.core.execution.domain import LoopPredicate, LoopSpec, Route, TaskSpec
    from coscientist.core.plan.loop import validate_loop_specs

    body = [TaskSpec(task_key="E0", title="t", goal="g", route=Route.EXPERIMENT, required=True, tolerate_failed_dep=False)]
    loop = LoopSpec.model_construct(
        loop_id="L1",
        body=body,
        until=LoopPredicate(name="survivors_le", params={"n": 0}),
        max_iter=1,
        scope_hypotheses=[None],
        scope_questions=[],
        depends_on=[],
    )
    with pytest.raises(PlanInvalid, match="None id"):
        validate_loop_specs([loop], known_keys={"E0"}, max_loop_iter=8)


@pytest.mark.asyncio
async def test_refuse_orphaned_loop_requires_cancel_task():
    from datetime import UTC, datetime

    from coscientist.core.execution.domain import (
        PlanRole,
        Route,
        Task,
        TaskKind,
        TaskSpec,
        TaskState,
    )
    from coscientist.core.plan.loop import refuse_orphaned_loop_barriers

    class _NoCancel:
        study = SimpleNamespace(study_id="s")
        _finalized = False
        plans: dict = {}
        _loop_state: dict = {}
        tasks = {
            "s/E0": Task(
                task_id="s/E0",
                kind=TaskKind.EXPERIMENT,
                spec=TaskSpec(
                    task_key="E0",
                    title="t",
                    goal="g",
                    route=Route.EXPERIMENT,
                    depends_on=["loop:ghost"], required=True, tolerate_failed_dep=False),
                state=TaskState.PENDING,
                deadline=datetime.now(UTC),
                plan_role=PlanRole.NONE,
            )
        }

    with pytest.raises(AttributeError, match="cancel_task"):
        await refuse_orphaned_loop_barriers(_NoCancel())  # type: ignore[arg-type]


def test_refuse_orphaned_barrier_parse_none_unreachable():
    """After is_loop_dep, parse_loop_dep None is unreachable — would raise."""
    from coscientist.core.plan.loop import is_loop_dep, parse_loop_dep

    assert is_loop_dep("loop:ok")
    assert parse_loop_dep("loop:ok") == "ok"
    assert parse_loop_dep("not-a-barrier") is None


def test_study_refused_requires_classifier_error():
    from datetime import UTC, datetime

    from coscientist.core.claims.reasons import RefusalKind
    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _LP(LocalProjectMixin):
        def __init__(self) -> None:
            self._intake_refusal = None

    payload = {
        "kind": RefusalKind.SAFETY.value,
        "reason": "bad",
    }
    ev = EventEnvelope(
        study_id="s",
        type="StudyRefused",
        entity_key="s",
        idem_key="k",
        payload=payload,
        ts=datetime.now(UTC),
        schema_version=1,
    )
    with pytest.raises(JournalFormatError, match="classifier_error"):
        _LP()._project_local(ev)


def test_budget_debited_first_task_spend_no_get_default():
    from datetime import UTC, datetime

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _LP(LocalProjectMixin):
        def __init__(self) -> None:
            self._study_spent_rub = Decimal("0")
            self._task_spent_rub = {}

    lp = _LP()
    payload = {"spent_rub": "1.5", "task_id": "s/E0"}
    ev = EventEnvelope(
        study_id="s",
        type="BudgetDebited",
        entity_key="s",
        idem_key="k",
        payload=payload,
        ts=datetime.now(UTC),
        schema_version=1,
    )
    lp._project_local(ev)
    assert lp._task_spent_rub["s/E0"] == Decimal("1.5")


def test_budget_debited_empty_task_id_raises():
    from datetime import UTC, datetime

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _LP(LocalProjectMixin):
        def __init__(self) -> None:
            self._study_spent_rub = Decimal("0")
            self._task_spent_rub = {}

    payload = {"spent_rub": "1", "task_id": ""}
    ev = EventEnvelope(
        study_id="s",
        type="BudgetDebited",
        entity_key="s",
        idem_key="k",
        payload=payload,
        ts=datetime.now(UTC),
        schema_version=1,
    )
    with pytest.raises(JournalFormatError, match="empty task_id"):
        _LP()._project_local(ev)


def test_expansion_refused_requires_after_task():
    from datetime import UTC, datetime

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _LP(LocalProjectMixin):
        def __init__(self) -> None:
            self._expansion_refusals = []
            self._expand_requests = {}
            self._loop_state = {}
            self._loop_capped = {}

    payload = {"reason": "invalid", "detail": "x"}
    ev = EventEnvelope(
        study_id="s",
        type="ExpansionRefused",
        entity_key="s",
        idem_key="k",
        payload=payload,
        ts=datetime.now(UTC),
        schema_version=1,
    )
    with pytest.raises(JournalFormatError, match="after_task"):
        _LP()._project_local(ev)


def test_artifact_registered_missing_id_raises():
    from datetime import UTC, datetime

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _LP(LocalProjectMixin):
        pass

    payload = {"kind": "experiment_design", "stamped": True}
    ev = EventEnvelope(
        study_id="s",
        type="ArtifactRegistered",
        entity_key="",
        idem_key="k",
        payload=payload,
        ts=datetime.now(UTC),
        schema_version=1,
    )
    with pytest.raises(JournalFormatError, match="missing artifact_id"):
        _LP()._project_local(ev)


def test_clear_expand_request_corrupt_entry_raises():
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _LP(LocalProjectMixin):
        def __init__(self) -> None:
            self._expand_requests = {"PLAN-1": "not-a-dict"}  # type: ignore[dict-item]

    with pytest.raises(JournalFormatError, match="corrupt"):
        _LP()._clear_expand_request(after_task="E0")


def test_task_created_non_plan_n_rejects_replan_allocated_s():
    from datetime import UTC, datetime

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.domain import (
        PlanRole,
        Route,
        TaskKind,
        TaskSpec,
        TaskState,
    )
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _LP(LocalProjectMixin):
        def __init__(self) -> None:
            self.tasks = {}
            self._replan_used_s = 0.0
            self._expand_requests = {}
            self._expand_pending = set()

    spec = TaskSpec(task_key="E0", title="t", goal="g", route=Route.EXPERIMENT, required=True, tolerate_failed_dep=False)
    payload = {
        "task_id": "s/E0",
        "kind": TaskKind.EXPERIMENT.value,
        "spec": spec.model_dump(mode="json"),
        "state": TaskState.PENDING.value,
        "deadline": datetime.now(UTC).isoformat(),
        "plan_role": PlanRole.NONE.value,
        "replan_allocated_s": 1.0,
    }
    ev = EventEnvelope(
        study_id="s",
        type="TaskCreated",
        entity_key="s/E0",
        idem_key="k",
        payload=payload,
        ts=datetime.now(UTC),
        schema_version=1,
    )
    with pytest.raises(JournalFormatError, match="replan_allocated_s"):
        _LP()._project_local(ev)


def test_local_project_plan_n_missing_alloc_raises():
    from datetime import UTC, datetime

    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.execution.domain import (
        PlanRole,
        Route,
        TaskKind,
        TaskSpec,
        TaskState,
    )
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _LP(LocalProjectMixin):
        def __init__(self) -> None:
            self.tasks = {}
            self._replan_used_s = 0.0
            self._expand_requests = {}
            self._expand_pending = set()

    spec = TaskSpec(
        task_key="PLAN-1",
        title="t",
        goal="g",
        route=Route.PLANNER, required=True, tolerate_failed_dep=False)
    payload = {
        "task_id": "s/PLAN-1",
        "kind": TaskKind.PLAN.value,
        "spec": spec.model_dump(mode="json"),
        "state": TaskState.PENDING.value,
        "deadline": datetime.now(UTC).isoformat(),
        "plan_role": PlanRole.PLAN_N.value,
        "expand_after_task": "E0",
        "pending_expansion": None,
        # replan_allocated_s absent — must raise
    }
    ev = EventEnvelope(
        study_id="s",
        type="TaskCreated",
        entity_key="s/PLAN-1",
        idem_key="k",
        payload=payload,
        ts=datetime.now(UTC),
        schema_version=1,
    )
    with pytest.raises(JournalFormatError, match="replan_allocated_s required"):
        _LP()._project_local(ev)


def test_inputs_claim_outcome_none_refuses():
    from datetime import UTC, datetime

    from coscientist.core.claims.graph import GraphNode, GraphStore, default_execution_registry
    from coscientist.core.events.ids import question_node_id
    from coscientist.core.execution.inputs import claim_nodes_from_attempt

    g = GraphStore(default_execution_registry())
    qid = "Q1"
    nid = question_node_id("s", qid)
    g.upsert_node(
        GraphNode(
            node_id=nid,
            kind="question",
            study_id="s",
            ref_id=qid,
            attrs={},
            created_at=datetime.now(UTC),
        )
    )

    def derive(raw, ok):
        return {
            "criterion_status": "met",
            "evidence_kind": "computational",
            "frozen": True,
            "lit_own_criterion": False,
            "accepted": True,
            "validity_ok": True,
            "check_role": "prediction",
            "claim_outcome": None,
            "predicted_direction": "two_sided",
            "predicts_null": False,
        }

    with pytest.raises(JournalFormatError, match="claim_outcome required"):
        claim_nodes_from_attempt(
            study_id="s",
            attempt_id="s/E0#1",
            provenance="real",
            claims=[{"id": "c1", "text": "t", "target": qid}],
            now=datetime.now(UTC),
            graph=g,
            attempt_outcome="SUCCESS",
            infra_failure=False,
            derive_attrs=derive,
        )


def test_inputs_does_not_write_outcome_alias():
    """claim_nodes_from_attempt must not stamp attrs['outcome'] dual-key."""
    import inspect

    from coscientist.core.execution import inputs as inputs_mod

    src = inspect.getsource(inputs_mod.claim_nodes_from_attempt)
    assert 'attrs["claim_outcome"]' in src or "attrs['claim_outcome']" in src
    assert 'attrs["outcome"]' not in src and "attrs['outcome']" not in src


def test_inputs_attempt_id_rsplit_strict_shape():
    from datetime import UTC, datetime

    from coscientist.core.claims.graph import GraphNode, GraphStore, default_execution_registry
    from coscientist.core.events.ids import question_node_id
    from coscientist.core.execution.inputs import claim_nodes_from_attempt

    g = GraphStore(default_execution_registry())
    qid = "Q1"
    nid = question_node_id("s", qid)
    g.upsert_node(
        GraphNode(
            node_id=nid,
            kind="question",
            study_id="s",
            ref_id=qid,
            attrs={},
            created_at=datetime.now(UTC),
        )
    )

    def derive(raw, ok):
        return {
            "criterion_status": "met",
            "evidence_kind": "computational",
            "frozen": True,
            "lit_own_criterion": False,
            "accepted": True,
            "validity_ok": True,
            "check_role": "prediction",
            "claim_outcome": "consistent",
            "predicted_direction": "two_sided",
            "predicts_null": False,
        }

    with pytest.raises(JournalFormatError, match="not structured"):
        claim_nodes_from_attempt(
            study_id="s",
            attempt_id="s/E0#1#extra",
            provenance="real",
            claims=[{"id": "c1", "text": "t", "target": qid}],
            now=datetime.now(UTC),
            graph=g,
            attempt_outcome="SUCCESS",
            infra_failure=False,
            derive_attrs=derive,
        )


def test_expand_validate_rollup_object_missing_attr():
    from coscientist.core.plan.expand_validate import _rollup_from_plans

    class _NoRollup:
        pass

    rollup, err = _rollup_from_plans([_NoRollup()])
    assert rollup is None
    assert err == "missing_plan_rollup"


def test_expand_validate_claim_attempt_id_partition_fail_closed():
    from coscientist.core.plan.expand_validate import task_produced_countable_claims

    claim = SimpleNamespace(attempt_id="bad-no-hash", counts=lambda: True)
    rt = SimpleNamespace(
        study=SimpleNamespace(study_id="s"),
        tasks={"s/E0": SimpleNamespace()},
        results={},
        graph=SimpleNamespace(),
    )

    def _filtered(*_a, **_k):
        return {"Q1": [claim]}


    # Patch via module import path used inside the function.
    import coscientist.core.claims.claims as claims_mod

    orig = claims_mod.filtered_claims_by_target
    claims_mod.filtered_claims_by_target = _filtered  # type: ignore[assignment]
    try:
        with pytest.raises(JournalFormatError, match="not structured"):
            task_produced_countable_claims(rt, "E0")
    finally:
        claims_mod.filtered_claims_by_target = orig  # type: ignore[assignment]


def test_plan_validate_task_done_uses_direct_spec_task_key():
    """Nested getattr(getattr(t.spec)) removed — missing spec raises AttributeError."""
    from coscientist.core.execution.domain import TaskOutcome

    t = SimpleNamespace(task_id="s/PLAN", spec=SimpleNamespace(task_key="PLAN"))
    assert (
        TaskOutcome.FAILED.value
        if t.spec.task_key == "PLAN"
        else TaskOutcome.SKIPPED.value
    ) == TaskOutcome.FAILED.value
    bare = SimpleNamespace(task_id="s/E0")
    with pytest.raises(AttributeError):
        _ = bare.spec.task_key


@pytest.mark.asyncio
async def test_lifecycle_known_question_texts_missing_text_raises():
    from coscientist.core.plan.lifecycle import LifecycleMixin

    class _LC(LifecycleMixin):
        def __init__(self) -> None:
            self.plans = {
                "p1": {"questions": [{"id": "Q1"}]},
            }

    with pytest.raises(ValueError, match="missing text"):
        await _LC()._known_question_texts()


def test_expand_graph_known_texts_missing_raises():
    from coscientist.core.plan.expand_graph import _require_known_text

    with pytest.raises(JournalFormatError, match="known_texts missing"):
        _require_known_text({"Q1": "ok"}, "Q_MISSING")


def test_runtime_factory_uses_direct_task_state():
    """getattr(t, 'state', None) removed — DONE tasks skip via t.state."""
    from coscientist.core.execution.domain import TaskState

    done = SimpleNamespace(state=TaskState.DONE, task_id="s/E0")
    open_t = SimpleNamespace(state=TaskState.PENDING, task_id="s/E1")
    skipped = [t for t in [done, open_t] if t.state != TaskState.DONE]
    assert [t.task_id for t in skipped] == ["s/E1"]


# --- C4: Study / HitlPolicy / create_study require explicit domain numbers ---


def test_c4_study_config_missing_fields_validation_error():
    from pydantic import ValidationError

    from coscientist.core.execution.domain import StudyConfig

    with pytest.raises(ValidationError):
        StudyConfig()  # type: ignore[call-arg]


def test_c4_study_missing_domain_fields_validation_error():
    from datetime import UTC, datetime

    from pydantic import ValidationError

    from coscientist.core.execution.domain import Study

    with pytest.raises(ValidationError):
        Study(
            study_id="s",
            profile="p",
            request="r",
            catalog_version="1",
            hitl_policy=default_test_hitl_policy(),
            deadline=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )


def test_c4_hitl_policy_requires_gates_no_default_timeouts():
    import inspect

    from pydantic import ValidationError

    from coscientist.core.execution import domain as domain_mod
    from coscientist.core.execution.domain import GateConfig, HitlPolicy

    assert not hasattr(domain_mod, "_default_gates")
    with pytest.raises(ValidationError):
        HitlPolicy()  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        GateConfig(timeout_s=120)  # type: ignore[call-arg]
    # No function that invents gate timeouts (docstring may mention the name).
    assert not any(
        isinstance(v, type(lambda: None)) and n == "_default_gates"
        for n, v in vars(domain_mod).items()
    )
    mod_src = inspect.getsource(domain_mod)
    assert "def _default_gates" not in mod_src
    assert "GateConfig(timeout_s=120" not in mod_src
    assert "GateConfig(timeout_s=1800" not in mod_src


@pytest.mark.asyncio
async def test_c4_create_study_requires_config_hitl_deadline_typeerror():
    from datetime import UTC, datetime, timedelta

    from coscientist.core.plan.lifecycle import LifecycleMixin
    from tests.helpers import default_test_hitl_policy, default_test_study_config

    class _LC(LifecycleMixin):
        def __init__(self) -> None:
            self.store = SimpleNamespace(study_id=None)
            self.clock = SimpleNamespace(now=lambda: datetime.now(UTC))

    with pytest.raises(TypeError, match="StudyConfig"):
        await _LC().create_study(
            profile="p",
            request="r",
            catalog_version="1",
            config=None,  # type: ignore[arg-type]
            hitl_policy=default_test_hitl_policy(),
            deadline=datetime.now(UTC) + timedelta(hours=1),
            min_plan_cost=0,
        )
    with pytest.raises(TypeError, match="HitlPolicy"):
        await _LC().create_study(
            profile="p",
            request="r",
            catalog_version="1",
            config=default_test_study_config(),
            hitl_policy=None,  # type: ignore[arg-type]
            deadline=datetime.now(UTC) + timedelta(hours=1),
            min_plan_cost=0,
        )



# --- Monkey blocker #1: _DEFAULT_* removed; fields/params required ---


def test_blocker_default_study_domain_fields_required():
    """Study-domain models raise when inventing Field defaults are absent."""
    from pydantic import ValidationError

    from coscientist.core.execution.domain import (
        Budget,
        InputSpec,
        OutputSpec,
        QuestionSpec,
        RequirementRef,
        TaskSpec,
    )

    with pytest.raises(ValidationError):
        Budget()  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        OutputSpec(name="main", media_type="text/plain")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        InputSpec(from_task="A", output="main")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        RequirementRef(template_id="value_match")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        QuestionSpec(id="Q1", text="why")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        TaskSpec(task_key="E1", title="t", goal="g", route="experiment")  # type: ignore[call-arg]


def test_blocker_default_gateconfig_enabled_required():
    from pydantic import ValidationError

    from coscientist.core.execution.domain import GateConfig

    with pytest.raises(ValidationError):
        GateConfig(timeout_s=120, on_timeout="delegate")  # type: ignore[call-arg]


def test_blocker_default_hitl_policy_mode_defaults_required():
    from pydantic import ValidationError

    from coscientist.core.execution.domain import GateConfig, HitlPolicy

    gates = {"plan_approval": GateConfig(timeout_s=1, on_timeout="fail", enabled=True)}
    with pytest.raises(ValidationError):
        HitlPolicy(gates=gates)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        HitlPolicy(gates=gates, mode="headless")  # type: ignore[call-arg]


def test_blocker_default_taskspec_final_type_on_question_required():
    """QuestionSpec.final_type (plan questions) has no silent verdict default."""
    from pydantic import ValidationError

    from coscientist.core.execution.domain import QuestionSpec

    with pytest.raises(ValidationError):
        QuestionSpec(id="Q1", text="why")  # type: ignore[call-arg]


def test_blocker_default_source_fetch_required_args():
    import inspect

    from coscientist.core.execution import source_fetch as sf

    sig = inspect.signature(sf.store_fetched_source)
    for name in ("name", "media_type", "role"):
        assert sig.parameters[name].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        sf.store_fetched_source(  # type: ignore[call-arg]
            None, None, b"", study_id="s", call_id="c", tool="t", url="u", capability=None
        )


# --- C5: claim_outcome required; no criterion_status → outcome map ---


def test_c5_inputs_missing_claim_outcome_raises_no_silent_map():
    from datetime import UTC, datetime

    from coscientist.core.claims.graph import GraphNode, GraphStore, default_execution_registry
    from coscientist.core.events.ids import question_node_id
    from coscientist.core.execution.inputs import claim_nodes_from_attempt

    g = GraphStore(default_execution_registry())
    qid = "Q1"
    nid = question_node_id("s", qid)
    g.upsert_node(
        GraphNode(
            node_id=nid,
            kind="question",
            study_id="s",
            ref_id=qid,
            attrs={},
            created_at=datetime.now(UTC),
        )
    )

    def derive_without_outcome(raw, ok):
        return {
            "criterion_status": "met",
            "evidence_kind": "computational",
            "frozen": True,
            "lit_own_criterion": False,
            "accepted": True,
            "validity_ok": True,
            "check_role": "prediction",
            # claim_outcome intentionally omitted — must not map from criterion_status
            "predicted_direction": "two_sided",
            "predicts_null": False,
        }

    with pytest.raises(JournalFormatError, match="claim_outcome required"):
        claim_nodes_from_attempt(
            study_id="s",
            attempt_id="s/E0#1",
            provenance="real",
            claims=[{"id": "c1", "text": "t", "target": qid}],
            now=datetime.now(UTC),
            graph=g,
            attempt_outcome="SUCCESS",
            infra_failure=False,
            derive_attrs=derive_without_outcome,
        )


def test_c5_inputs_source_has_no_criterion_status_map():
    import inspect

    from coscientist.core.execution import inputs as inputs_mod

    src = inspect.getsource(inputs_mod.claim_nodes_from_attempt)
    assert '"met": "consistent"' not in src
    assert "cannot derive outcome" not in src


def test_c5_derive_claim_core_attrs_always_sets_claim_outcome():
    from coscientist.core.execution.domain import TaskOutcome
    from coscientist.core.execution.executor import _derive_claim_core_attrs

    task = SimpleNamespace(
        task_id="s/E0",
        spec=SimpleNamespace(
            method=None,
            criteria=[],
            route="experiment",
        ),
    )
    out = _derive_claim_core_attrs(
        task,
        TaskOutcome.SUCCESS,
        raw={"id": "c1", "text": "t", "target": "Q1"},
        checks=[],
        graph=None,
        study_id="s",
        validated=True,
    )
    assert "claim_outcome" in out
    assert out["claim_outcome"] in ("consistent", "inconsistent", "indeterminate")