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

    task = SimpleNamespace(
        outcome=TaskOutcome.SUCCESS,
        spec=SimpleNamespace(task_key="E0"),
        state=TaskState.DONE,
        kind=TaskKind.EXPERIMENT,
        task_id="s/E0",
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
        store_mod.SqliteEventStore("s", tmp_path / "locked.db")


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

    out = await expand_mod.expand_plan(
        rt,
        after_task="E0",
        added_tasks=[
            TaskSpec(
                task_key="E1",
                title="t",
                goal="g",
                route="experiment",
            )
        ],
        origin="emergent",
    )
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
            route=Route.PLANNER,
        ),
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
            route=Route.PLAN,
        ),
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

    store = SqliteEventStore("s_sf", tmp_path / "sf.db", owner="t")
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
