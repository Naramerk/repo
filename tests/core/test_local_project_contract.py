"""LocalProjectMixin fold contract: host attributes exist before folding.

Fold methods in ``coscientist.core.execution.local_project`` read their state
attributes directly; ``StudyRuntime.__init__`` is the single place that must
assign every name declared on the mixin before any event fold runs.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.events import EventEnvelope, EventType, make_idem
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import Task
from coscientist.core.execution.local_project import LocalProjectMixin
from coscientist.core.execution.machine import StudyRuntime, new_owner
from tests.checks.minimal_test_catalog import minimal_test_catalog


def _fresh_runtime(tmp_path: Path, clock: FakeClock) -> StudyRuntime:
    store = SqliteEventStore("st_mixin_contract", tmp_path / "e.db", clock=clock)
    return StudyRuntime(
        store,
        StudyWriter(store),
        clock=clock,
        owner=new_owner(),
        catalog=minimal_test_catalog(),
    )


def test_study_runtime_initializes_every_mixin_attribute(tmp_path, clock) -> None:
    """Direct reads in fold methods require __init__ to assign each mixin attr."""
    rt = _fresh_runtime(tmp_path, clock)
    missing = [name for name in LocalProjectMixin.__annotations__ if name not in vars(rt)]
    assert missing == []


def _flush(host: LocalProjectMixin) -> None:
    host._flush_pending_kind_stamps()


def _clear(host: LocalProjectMixin) -> None:
    host._clear_expand_request()


def _stamp(host: LocalProjectMixin) -> None:
    host._apply_kind_stamp_soft("art_1", "experiment_design")


@pytest.mark.parametrize(
    ("fold_call", "attr"),
    [
        pytest.param(_flush, "_pending_kind_stamps", id="flush-pending-kind-stamps"),
        pytest.param(_clear, "_expand_requests", id="clear-expand-request"),
        pytest.param(_stamp, "artifacts", id="apply-kind-stamp-soft"),
    ],
)
def test_fold_without_initialized_host_fails_fast(
    fold_call: Callable[[LocalProjectMixin], None], attr: str
) -> None:
    """A host that skips attribute initialization is a bug: the fold fails loudly."""
    with pytest.raises(
        AttributeError,
        match=re.escape(f"'LocalProjectMixin' object has no attribute '{attr}'"),
    ):
        fold_call(LocalProjectMixin())


_FOLD_TS = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

_STUDY_PAYLOAD: dict[str, object] = {
    "study_id": "st_mixin_contract",
    "profile": "test",
    "request": "test request",
    "catalog_version": 1,
    "hitl_policy": {},
    "deadline": "2026-12-01T00:00:00+00:00",
    "created_at": "2026-10-30T00:00:00+00:00",
    "confirmations_needed": 1,
    "refutations_needed": 1,
    "refutation_priority": True,
    "supported_needed": 2,
    "max_expansions": 1,
    "max_attempts": 2,
    "require_hypotheses": False,
}

_TASK_PAYLOAD: dict[str, object] = {
    "task_id": "task_1",
    "kind": "spec",
    "spec": {"task_key": "task_1", "title": "T", "goal": "G", "route": "local"},
    "state": "PENDING",
    "deadline": "2026-12-01T00:00:00+00:00",
}

_PLAN_TASK_PAYLOAD: dict[str, object] = {
    **_TASK_PAYLOAD,
    "task_id": "plan_2",
    "spec": {"task_key": "PLAN-2", "title": "T", "goal": "G", "route": "local"},
    "expand_after_task": "task_1",
}

_STAMP_PAYLOAD: dict[str, object] = {
    "artifact_id": "art_1",
    "kind": "experiment_design",
    "stamped": True,
}


def _envelope(event_type: EventType, payload: dict[str, object]) -> EventEnvelope:
    """Build a real journal envelope of ``event_type`` for a fold contract case."""
    return EventEnvelope(
        study_id="st_mixin_contract",
        entity_key="entity_1",
        idem_key=make_idem(event_type, "entity_1"),
        type=event_type,
        payload=payload,
        ts=_FOLD_TS,
        schema_version=1,
    )


def _host_plan_expanded() -> LocalProjectMixin:
    host = LocalProjectMixin()
    host.plans = {}
    host._expand_requests = {}
    return host


def _host_plan_expanded_agent() -> LocalProjectMixin:
    host = _host_plan_expanded()
    host._agent_expansions = 0
    return host


def _case_study_created() -> tuple[LocalProjectMixin, EventEnvelope]:
    return LocalProjectMixin(), _envelope("StudyCreated", _STUDY_PAYLOAD)


def _case_budget_debited() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host._study_spent_rub = Decimal("0")
    return host, _envelope("BudgetDebited", {"spent_rub": "5", "task_id": "task_1"})


def _case_plan_expanded_human() -> tuple[LocalProjectMixin, EventEnvelope]:
    return _host_plan_expanded(), _envelope("PlanExpanded", {"delegated_by": "human"})


def _case_plan_expanded_agent() -> tuple[LocalProjectMixin, EventEnvelope]:
    return _host_plan_expanded(), _envelope("PlanExpanded", {})


def _case_plan_expanded_replan() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = _host_plan_expanded_agent()
    return host, _envelope("PlanExpanded", {"replan_allocated_s": 1.5})


def _case_plan_expanded_after_task() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = _host_plan_expanded_agent()
    return host, _envelope("PlanExpanded", {"after_task": "task_1"})


def _case_plan_expanded_hitl() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = _host_plan_expanded_agent()
    return host, _envelope("PlanExpanded", {"hitl_request_id": "req_1"})


def _case_plan_expanded_foreach() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = _host_plan_expanded()
    return host, _envelope(
        "PlanExpanded",
        {
            "foreach": [1],
            "foreach_template": "tmpl",
            "added_tasks": [{"task_key": "kid_1"}],
        },
    )


def _case_expansion_refused() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host._expand_requests = {}
    return host, _envelope("ExpansionRefused", {})


def _case_hitl_opened() -> tuple[LocalProjectMixin, EventEnvelope]:
    return LocalProjectMixin(), _envelope("HitlOpened", {"request_id": "req_1"})


def _case_hitl_decided_decisions() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host._hitl_requests = {}
    return host, _envelope("HitlDecided", {"request_id": "req_1"})


def _case_hitl_decided_opens() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host._hitl_decisions = {}
    return host, _envelope("HitlDecided", {"request_id": "req_1"})


def _case_artifact_registered_artifacts() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host._pending_kind_stamps = []
    return host, _envelope("ArtifactRegistered", _STAMP_PAYLOAD)


def _case_artifact_registered_pending() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host.artifacts = None
    return host, _envelope("ArtifactRegistered", _STAMP_PAYLOAD)


def _case_question_disposed() -> tuple[LocalProjectMixin, EventEnvelope]:
    return LocalProjectMixin(), _envelope("QuestionDisposed", {})


def _case_question_dispose_rejected() -> tuple[LocalProjectMixin, EventEnvelope]:
    return LocalProjectMixin(), _envelope("QuestionDisposeRejected", {})


def _case_study_finalized_duplicate() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host._finalized = True
    return host, _envelope("StudyFinalized", {})


def _case_task_created_replan() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host.tasks = {}
    return host, _envelope("TaskCreated", {**_TASK_PAYLOAD, "replan_allocated_s": 2.0})


def _case_task_created_requests() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host.tasks = {}
    host._expand_pending = set()
    return host, _envelope("TaskCreated", _PLAN_TASK_PAYLOAD)


def _case_task_created_pending() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host.tasks = {}
    host._expand_requests = {}
    return host, _envelope("TaskCreated", _PLAN_TASK_PAYLOAD)


def _case_attempt_started_alloc() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host.attempts = {}
    host.tasks = {"task_1": Task.model_validate(_TASK_PAYLOAD)}
    host._retry_at = {}
    return host, _envelope(
        "AttemptStarted",
        {"attempt_id": "att_1", "task_id": "task_1", "allocated_budget_rub": "10"},
    )


def _case_result_recorded_dep_outputs() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host.results = {}
    host.attempts = {}
    host.dep_provenance = {}
    return host, _envelope("ResultRecorded", {"result_id": "res_1", "attempt_id": "att_1"})


def _case_result_recorded_dep_provenance() -> tuple[LocalProjectMixin, EventEnvelope]:
    host = LocalProjectMixin()
    host.results = {}
    host.attempts = {}
    host.dep_outputs = {}
    return host, _envelope("ResultRecorded", {"result_id": "res_1", "attempt_id": "att_1"})


@pytest.mark.parametrize(
    ("make_case", "attr"),
    [
        pytest.param(_case_study_created, "executor", id="study-created-executor"),
        pytest.param(
            _case_budget_debited, "_task_spent_rub", id="budget-debited-task-spent"
        ),
        pytest.param(
            _case_plan_expanded_human,
            "_human_expansions",
            id="plan-expanded-human-expansions",
        ),
        pytest.param(
            _case_plan_expanded_agent,
            "_agent_expansions",
            id="plan-expanded-agent-expansions",
        ),
        pytest.param(
            _case_plan_expanded_replan,
            "_replan_used_s",
            id="plan-expanded-replan-used",
        ),
        pytest.param(
            _case_plan_expanded_after_task,
            "_expand_pending",
            id="plan-expanded-expand-pending",
        ),
        pytest.param(
            _case_plan_expanded_hitl,
            "_hitl_expand_used",
            id="plan-expanded-hitl-expand-used",
        ),
        pytest.param(
            _case_plan_expanded_foreach,
            "_foreach_children",
            id="plan-expanded-foreach-children",
        ),
        pytest.param(
            _case_expansion_refused,
            "_expansion_refusals",
            id="expansion-refused-refusals",
        ),
        pytest.param(
            _case_hitl_opened, "_hitl_requests", id="hitl-opened-open-requests"
        ),
        pytest.param(
            _case_hitl_decided_decisions,
            "_hitl_decisions",
            id="hitl-decided-decisions",
        ),
        pytest.param(
            _case_hitl_decided_opens,
            "_hitl_requests",
            id="hitl-decided-open-requests",
        ),
        pytest.param(
            _case_artifact_registered_artifacts,
            "artifacts",
            id="artifact-registered-artifacts",
        ),
        pytest.param(
            _case_artifact_registered_pending,
            "_pending_kind_stamps",
            id="artifact-registered-pending-stamps",
        ),
        pytest.param(
            _case_question_disposed,
            "question_disposals",
            id="question-disposed-store",
        ),
        pytest.param(
            _case_question_dispose_rejected,
            "_disposal_rejects",
            id="question-dispose-rejected-notes",
        ),
        pytest.param(
            _case_study_finalized_duplicate,
            "_duplicate_study_finalized",
            id="study-finalized-duplicates",
        ),
        pytest.param(
            _case_task_created_replan,
            "_replan_used_s",
            id="task-created-replan-used",
        ),
        pytest.param(
            _case_task_created_requests,
            "_expand_requests",
            id="task-created-expand-requests",
        ),
        pytest.param(
            _case_task_created_pending,
            "_expand_pending",
            id="task-created-expand-pending",
        ),
        pytest.param(
            _case_attempt_started_alloc,
            "_task_budget_alloc",
            id="attempt-started-budget-alloc",
        ),
        pytest.param(
            _case_result_recorded_dep_outputs,
            "dep_outputs",
            id="result-recorded-dep-outputs",
        ),
        pytest.param(
            _case_result_recorded_dep_provenance,
            "dep_provenance",
            id="result-recorded-dep-provenance",
        ),
    ],
)
def test_project_local_branch_without_host_attr_fails_fast(
    make_case: Callable[[], tuple[LocalProjectMixin, EventEnvelope]], attr: str
) -> None:
    """Folding a real envelope on a host missing one mixin attribute raises."""
    host, ev = make_case()
    with pytest.raises(
        AttributeError,
        match=re.escape(f"'LocalProjectMixin' object has no attribute '{attr}'"),
    ):
        host._project_local(ev)


def _host_stamp_soft(tmp_path: Path) -> LocalProjectMixin:
    host = LocalProjectMixin()
    host.artifacts = LocalArtifactStore(tmp_path / "artifacts")
    host._stamp_meta_missing = []
    return host


def _host_stamp_soft_kinds(tmp_path: Path) -> LocalProjectMixin:
    host = LocalProjectMixin()
    host.artifacts = LocalArtifactStore(tmp_path / "artifacts")
    host.artifact_kinds = {}
    return host


def _host_flush_pending(tmp_path: Path) -> LocalProjectMixin:
    host = _host_stamp_soft(tmp_path)
    host._pending_kind_stamps = [("art_1", "experiment_design")]
    return host


def _apply_stamp(host: LocalProjectMixin) -> None:
    host._apply_kind_stamp_soft("art_1", "experiment_design")


def _flush_stamps(host: LocalProjectMixin) -> None:
    host._flush_pending_kind_stamps()


@pytest.mark.parametrize(
    ("make_host", "fold_call", "attr"),
    [
        pytest.param(
            _host_stamp_soft,
            _apply_stamp,
            "artifact_kinds",
            id="apply-kind-stamp-soft-artifact-kinds",
        ),
        pytest.param(
            _host_stamp_soft_kinds,
            _apply_stamp,
            "_stamp_meta_missing",
            id="apply-kind-stamp-soft-stamp-meta-missing",
        ),
        pytest.param(
            _host_flush_pending,
            _flush_stamps,
            "artifact_kinds",
            id="flush-pending-kind-stamps-artifact-kinds",
        ),
    ],
)
def test_helper_without_host_attr_fails_fast(
    make_host: Callable[[Path], LocalProjectMixin],
    fold_call: Callable[[LocalProjectMixin], None],
    attr: str,
    tmp_path: Path,
) -> None:
    """Helpers that stamp kinds read artifact_kinds directly (no self-heal)."""
    with pytest.raises(
        AttributeError,
        match=re.escape(f"'LocalProjectMixin' object has no attribute '{attr}'"),
    ):
        fold_call(make_host(tmp_path))
