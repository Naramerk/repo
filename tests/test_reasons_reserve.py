"""PR 1: TaskReason/CloseReason, provenance stub, study_view, finalize reserve."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import pytest

from coscientist.core.claims.reasons import (
    CloseReason,
    Disposition,
    NotAttemptedReason,
    RefusalKind,
    TaskReason,
    TerminalReason,
    coerce_task_reason,
    intake,
)
from coscientist.core.events.errors import CatalogError
from coscientist.core.events.events import (
    STUDY_CREATED_SCHEMA_VERSION,
    EventEnvelope,
    make_idem,
)
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.upcasters import upcast
from coscientist.core.execution.domain import (
    HitlPolicy,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.finalize.study_view import study_view
from coscientist.core.plan.intake import IntakeRefuse
from tests.helpers import close_runtime, open_runtime, seed_legacy_journal

# Legacy reason strings from §0.1 that map to TaskReason / CloseReason.
_TASK_REASON_STRINGS = [
    "failed",
    "deadline",
    "study_deadline",
    "cancelled",
    "budget",
    "input_unavailable",
    "human",
    "human_rejected",
    "critic_rejected",
    "human_timeout",
    "catalog_error",
    "sandbox_infra",
    "sandbox_timeout",
    "idle_timeout",
    "same_digest_retry",
    "core_bug",
    "schema_invalid",
    "invalid_status",
    "retry_scheduled",
    "superseded",
    "expansion_limit",
    "iteration_cap",
    "refused",
    "crashed",

]
_CLOSE_REASON_STRINGS = [
    "result",
    "failed",
    "crashed",
    "deadline",
    "study_deadline",
    "cancelled",
    "input_unavailable",
    "human_timeout",
    "budget",
]


@pytest.mark.parametrize("reason", _TASK_REASON_STRINGS, ids=_TASK_REASON_STRINGS)
def test_task_reason_enum_roundtrip(reason: str):
    assert TaskReason(reason).value == reason


@pytest.mark.parametrize("reason", _CLOSE_REASON_STRINGS, ids=_CLOSE_REASON_STRINGS)
def test_close_reason_enum_roundtrip(reason: str):
    assert CloseReason(reason).value == reason


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        (TerminalReason.COMPLETED, "completed"),
        (Disposition.NOT_ATTEMPTED, "not_attempted"),
        (NotAttemptedReason.NO_TASK, "no_task"),
        (RefusalKind.BUDGET_ZERO, "budget_zero"),
        (RefusalKind.DURATION_TOO_SHORT, "duration_too_short"),
    ],
    ids=[
        "terminal-completed",
        "disposition-not-attempted",
        "not-attempted-no-task",
        "refusal-budget-zero",
        "refusal-duration-too-short",
    ],
)
def test_declared_only_enum_values(member: StrEnum, expected: str):
    # Declared-only enums for later PRs already carry their wire values.
    assert member.value == expected


def test_intake_empty_request_refuses():
    refused = asyncio.run(intake("", [], "p", None))
    assert isinstance(refused, IntakeRefuse)
    assert refused.kind == RefusalKind.EMPTY_REQUEST


@pytest.mark.parametrize("reason", _TASK_REASON_STRINGS, ids=_TASK_REASON_STRINGS)
def test_upcast_every_legacy_reason(reason: str):
    v, payload = upcast("TaskDone", 1, {"task_id": "t", "outcome": "FAILED", "reason": reason})
    assert v == 2
    assert payload["reason"] == reason


@pytest.mark.parametrize("reason", _CLOSE_REASON_STRINGS, ids=_CLOSE_REASON_STRINGS)
def test_upcast_every_legacy_close_reason(reason: str):
    v, p = upcast(
        "AttemptClosed",
        1,
        {"attempt_id": "a", "closed": True, "close_reason": reason},
    )
    assert v == 2
    assert p["close_reason"] == reason


def test_upcast_task_done_unknown_reason_raises():
    with pytest.raises(ValueError, match=re.escape("unknown TaskDone.reason")):
        upcast("TaskDone", 1, {"task_id": "t", "outcome": "FAILED", "reason": "nope"})


def test_upcast_attempt_closed_unknown_reason_raises():
    with pytest.raises(ValueError, match=re.escape("unknown AttemptClosed.close_reason")):
        upcast(
            "AttemptClosed",
            1,
            {"attempt_id": "a", "closed": True, "close_reason": "nope"},
        )


class _ReasonStrSubclass(str):
    """Plain ``str`` subclass (not an enum) carrying a legacy reason value."""


class _ForeignReason(StrEnum):
    """StrEnum unrelated to TaskReason/CloseReason (foreign vocabulary)."""

    FAILED = "failed"


_STR_SUBCLASS_REASON_CASES: list[tuple[object, str]] = [
    (TaskReason.FAILED, "failed"),
    (CloseReason.FAILED, "failed"),
    (_ReasonStrSubclass("failed"), "failed"),
    (_ForeignReason.FAILED, "failed"),
]
_STR_SUBCLASS_REASON_IDS = [
    "task-enum-member",
    "close-enum-member",
    "str-subclass",
    "foreign-strenum",
]
_NON_STR_REASON_CASES: list[object] = [
    42,
    True,
    3.14,
    b"failed",
    ["failed"],
    {"reason": "failed"},
]
_NON_STR_REASON_IDS = ["int", "bool", "float", "bytes", "list", "dict"]


@pytest.mark.parametrize(
    ("raw", "expected"), _STR_SUBCLASS_REASON_CASES, ids=_STR_SUBCLASS_REASON_IDS
)
def test_upcast_task_done_reason_accepts_str_subclass_values(raw: object, expected: str):
    """StrEnum members are str subclasses; TaskDone upcast coerces them like plain strings."""
    _, p1 = upcast("TaskDone", 1, {"task_id": "t", "outcome": "FAILED", "reason": raw})
    assert p1["reason"] == expected


@pytest.mark.parametrize(
    ("raw", "expected"), _STR_SUBCLASS_REASON_CASES, ids=_STR_SUBCLASS_REASON_IDS
)
def test_upcast_attempt_closed_reason_accepts_str_subclass_values(raw: object, expected: str):
    """StrEnum members are str subclasses; AttemptClosed upcast coerces them like strings."""
    _, p2 = upcast(
        "AttemptClosed",
        1,
        {"attempt_id": "a", "closed": True, "close_reason": raw},
    )
    assert p2["close_reason"] == expected


@pytest.mark.parametrize("raw", _NON_STR_REASON_CASES, ids=_NON_STR_REASON_IDS)
def test_upcast_task_done_reason_non_str_raises(raw: object):
    with pytest.raises(ValueError, match=re.escape("unknown TaskDone.reason")) as info:
        upcast("TaskDone", 1, {"task_id": "t", "outcome": "FAILED", "reason": raw})
    assert isinstance(info.value.__cause__, ValueError)


@pytest.mark.parametrize("raw", _NON_STR_REASON_CASES, ids=_NON_STR_REASON_IDS)
def test_upcast_attempt_closed_reason_non_str_raises(raw: object):
    with pytest.raises(ValueError, match=re.escape("unknown AttemptClosed.close_reason")) as info:
        upcast(
            "AttemptClosed",
            1,
            {"attempt_id": "a", "closed": True, "close_reason": raw},
        )
    assert isinstance(info.value.__cause__, ValueError)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (TaskReason.FAILED, TaskReason.FAILED),
        ("failed", TaskReason.FAILED),
        ("cancelled", TaskReason.CANCELLED),
    ],
    ids=["none", "enum", "failed-str", "cancelled-str"],
)
def test_coerce_task_reason_accepts_known(value, expected):
    assert coerce_task_reason(value) == expected


@pytest.mark.parametrize(
    "raw",
    ["nope", "FAIL", "failed ", "budget_exhausted", "cancel"],
    ids=["unknown", "case", "whitespace", "alias-like", "truncated"],
)
def test_coerce_task_reason_unknown_raises(raw):
    """No aliases / guessing: unknown strings raise ValueError (fail-closed)."""
    with pytest.raises(ValueError, match=re.escape("not a valid TaskReason")):
        coerce_task_reason(raw)


@pytest.mark.asyncio
async def test_writer_emits_v2(tmp_path: Path, clock, runtime_cls):
    rt = await open_runtime(tmp_path, "st_v2", clock=clock, runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_v2",
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    await rt.cancel_task("SPEC", reason=TaskReason.CANCELLED)
    events = await rt.store.read_all()
    done = [e for e in events if e.type == "TaskDone"]
    closed = [e for e in events if e.type == "AttemptClosed"]
    assert done, "expected TaskDone events after cancel_task"
    for e in done:
        assert e.schema_version == 2, f"{e.type} {e.entity_key}: schema_version {e.schema_version}"
        assert e.payload.get("reason") in (None, "cancelled"), (
            f"TaskDone {e.entity_key}: unexpected reason {e.payload.get('reason')!r}"
        )
    # SPEC had no open attempt — cancel may not emit AttemptClosed
    for e in closed:
        assert e.schema_version == 2, f"{e.type} {e.entity_key}: schema_version {e.schema_version}"
    await close_runtime(rt)


def test_upcast_attempt_started_simulated_to_stub():
    v, p = upcast("AttemptStarted", 1, {"provenance": "simulated", "attempt_id": "a"})
    # …v9 attempt_role → v10 provenance → v11 AttemptUnrecorded for omitted forever-fields.
    assert v == 11
    assert p["provenance"] == "stub"
    assert p["inputs"] == []
    assert p["limitations"] == []
    assert p["allocated_budget_rub"] is None
    assert p["budget_limit_rub"] is None
    assert p["tainted_context"] is False
    assert p["attempt_role"] == "agent"
    assert p.get("inputs_digest") is None
    assert p["n"] == {"kind": "unrecorded", "source_schema_version": 10}


def test_upcast_result_recorded_simulated_to_stub():
    v, p = upcast(
        "ResultRecorded",
        1,
        {
            "provenance": "simulated",
            "inputs": [{"input": "x", "artifact_id": "a", "source": "simulated"}],
            "verdict": "simulated",
        },
    )
    # v1→v2 simulated→stub, then v2→v3 output_kinds={} (PR8b).
    assert v == 3
    assert p["provenance"] == "stub"
    assert p["inputs"][0]["source"] == "stub"
    assert p["verdict"] == "stubbed"
    assert p["output_kinds"] == {}


def test_upcast_attempt_started_v9_to_v10_keeps_partial():
    """v9→v10: explicit partial provenance is preserved (no invent real); v11 Unrecorded."""
    v, p = upcast(
        "AttemptStarted",
        9,
        {"attempt_id": "a", "provenance": "partial"},
    )
    assert v == 11
    assert p["provenance"] == "partial"
    assert p["n"] == {"kind": "unrecorded", "source_schema_version": 10}


@pytest.mark.parametrize(
    ("from_version", "payload", "match"),
    [
        (9, {"attempt_id": "a"}, "provenance_required"),
        (9, {"attempt_id": "a", "provenance": None}, "provenance_required"),
        (9, {"attempt_id": "a", "provenance": "simulated"}, "provenance_unknown"),
        (1, {"attempt_id": "legacy"}, "provenance_required"),
    ],
    ids=["absent", "null", "unknown", "legacy-chain"],
)
def test_upcast_attempt_started_v9_to_v10_provenance_required(from_version, payload, match):
    """v9→v10: absent / null / unknown provenance → CatalogError (no invent real)."""
    with pytest.raises(CatalogError, match=re.escape(match)):
        upcast("AttemptStarted", from_version, payload)


def _task(
    key: str,
    *,
    required: bool = True,
    outcome: TaskOutcome = TaskOutcome.SUCCESS,
    attempts: list[str] | None = None,
) -> Task:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Task(
        task_id=f"st/{key}",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key=key,
            title=key,
            goal="g",
            route="experiment",
            required=required,
        ),
        state=TaskState.DONE,
        outcome=outcome,
        deadline=now,
        attempts=attempts or [],
    )


_STUDY_VIEW_CASES = [
    pytest.param(
        [
            _task("A", attempts=["a1"]),
            _task("B", outcome=TaskOutcome.PARTIAL, attempts=["a2"]),
        ],
        {
            "a1": {"provenance": "real", "closed": True},
            "a2": {"provenance": "stub", "closed": True},
        },
        "stubbed",
        0.5,
        id="required-stub-marks-stubbed",
    ),
    pytest.param(
        [
            _task("R", attempts=["r1"]),
            _task("O", required=False, attempts=["o1"]),
        ],
        {
            "r1": {"provenance": "real", "closed": True},
            "o1": {"provenance": "stub", "closed": True},
        },
        "fulfilled",
        0.5,
        id="optional-stub-only-still-fulfilled",
    ),
]


@pytest.mark.parametrize(("tasks", "atts", "verdict", "stub_share"), _STUDY_VIEW_CASES)
def test_study_view_verdict_and_stub_share(
    tasks: list[Task],
    atts: dict[str, dict[str, object]],
    verdict: str,
    stub_share: float,
):
    view = study_view(tasks, attempts=atts)
    assert view.verdict == verdict
    assert view.stub_share == stub_share


_LAST_CLOSED_ATTEMPT_CASES = [
    pytest.param(
        TaskOutcome.SUCCESS,
        ["a1", "a2"],
        {
            "a1": {"provenance": "stub", "closed": True},
            "a2": {"provenance": "real", "closed": True},
        },
        "fulfilled",
        0.0,
        id="last-closed-real-after-earlier-stub",
    ),
    pytest.param(
        TaskOutcome.PARTIAL,
        ["a1", "a2"],
        {
            "a1": {"provenance": "real", "closed": True},
            "a2": {"provenance": "stub", "closed": True},
        },
        "stubbed",
        1.0,
        id="last-closed-stub-marks-stubbed",
    ),
    pytest.param(
        TaskOutcome.SUCCESS,
        ["a1", "a2"],
        {
            "a1": {"provenance": "real", "closed": True},
            "a2": {"provenance": "stub", "closed": False},
        },
        "fulfilled",
        0.0,
        id="open-stub-after-real-closed",
    ),
]


@pytest.mark.parametrize(
    ("outcome", "attempts", "atts", "verdict", "stub_share"),
    _LAST_CLOSED_ATTEMPT_CASES,
)
def test_task_is_stub_uses_last_closed_attempt(
    outcome: TaskOutcome,
    attempts: list[str],
    atts: dict[str, dict[str, object]],
    verdict: str,
    stub_share: float,
):
    """Which attempt counts is decided by the last closed one, not the first."""
    view = study_view([_task("A", outcome=outcome, attempts=attempts)], attempts=atts)
    assert view.verdict == verdict
    assert view.stub_share == stub_share


@pytest.mark.asyncio
async def test_create_study_rejects_duration_le_reserve(tmp_path: Path, clock, runtime_cls):
    """duration ≤ reserve → intake Refuse (StudyRefused), not ValueError."""
    rt = await open_runtime(tmp_path, "st_rej", clock=clock, runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=30),
        sid="st_rej",
        finalize_reserve_s=30,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "StudyRefused"]
    assert refused, "expected a StudyRefused event"
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert fins, "expected a StudyFinalized event"
    assert fins[0].payload["trigger"] == "intake"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_create_study_rejects_negative_reserve(tmp_path: Path, clock, runtime_cls):
    rt = await open_runtime(tmp_path, "st_neg", clock=clock, runtime_cls=runtime_cls)
    with pytest.raises(ValueError, match=re.escape("finalize_reserve_s must be >= 0")):
        await rt.create_study(
            profile="research",
            request="q",
            catalog_version=4,
            deadline=clock.now() + timedelta(minutes=30),
            sid="st_neg",
            finalize_reserve_s=-1,
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            inputs=[],
        )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_all_deadlines_le_effective(tmp_path: Path, clock, runtime_cls):
    rt = await open_runtime(tmp_path, "st_eff", clock=clock, runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(minutes=10),
        sid="st_eff",
        finalize_reserve_s=30,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    assert rt.study is not None
    ed = rt.study.effective_deadline()
    assert ed == rt.study.deadline - timedelta(seconds=30)
    for t in rt.tasks.values():
        assert t.deadline <= ed, f"{t.task_id}: deadline {t.deadline} > effective {ed}"
    assert rt.invariant_deadlines()
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_plan_deadline_clipped_by_reserve(tmp_path: Path, clock, runtime_cls):
    rt = await open_runtime(tmp_path, "st_plan", clock=clock, runtime_cls=runtime_cls)
    # Study shorter than 600s plan window → plan clipped to effective_deadline
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=120),
        sid="st_plan",
        finalize_reserve_s=30,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    assert rt.study is not None
    ed = rt.study.effective_deadline()
    plan = rt.tasks["st_plan/PLAN"]
    report = rt.tasks["st_plan/REPORT"]
    assert plan.deadline == ed
    assert report.deadline == ed
    await close_runtime(rt)


def test_upcast_preserves_null_reason():
    v, p = upcast("TaskDone", 1, {"task_id": "t", "outcome": "SUCCESS", "reason": None})
    assert v == 2
    assert p["reason"] is None


def test_upcast_study_created_sets_reserve_zero():
    v, p = upcast(
        "StudyCreated",
        1,
        {
            "study_id": "st_old",
            "profile": "research",
            "request": "q",
            "catalog_version": 4,
            "hitl_policy": {},
            "deadline": "2026-01-01T01:00:00+00:00",
            "created_at": "2026-01-01T00:00:00+00:00",
            "require_hypotheses": False,
            "confirmations_needed": 1,
            "refutations_needed": 1,
            "refutation_priority": False,
            "supported_needed": 2,
            "max_expansions": 2,
            "max_attempts": 3,
        },
    )
    # v1→…→v10 max_loop_iter (PR11) → v11 as_of (#23) → v12 max_proposals (PR20).
    assert v == STUDY_CREATED_SCHEMA_VERSION
    assert p["max_foreach_children"] == 32
    assert p["max_loop_iter"] == 8
    assert p.get("as_of") is None
    assert p.get("user_input_ids") == []
    assert p["finalize_reserve_s"] == 0
    assert p["refutations_needed"] == 1
    assert p["refutation_priority"] is False
    assert p["supported_needed"] == 2
    assert p["confirmations_needed"] == 1
    assert p["aggregate"] == "count"
    assert p["max_expansions"] == 2
    assert p["replan_reserve"] == 0.1
    assert p["as_of"] is None
    assert p["user_input_ids"] == []
    assert p["max_proposals"] == 3
    # Explicit value must not be overwritten
    v2, p2 = upcast(
        "StudyCreated",
        1,
        {
            "catalog_version": 4,
            **p,
            "finalize_reserve_s": 30,
            "require_hypotheses": False,
            "max_attempts": 3,
        },
    )
    assert p2["finalize_reserve_s"] == 30


@pytest.mark.asyncio
async def test_old_journal_replay_keeps_deadlines_and_tick(tmp_path: Path, clock, runtime_cls):
    """StudyCreated v1 without finalize_reserve_s → reserve=0; deadlines unchanged; tick works."""
    sid = "st_legacy"
    root = tmp_path / sid
    root.mkdir(parents=True)
    store = SqliteEventStore(sid, root / "events.db", clock=clock)
    await store.acquire_lease("seeder")
    now = clock.now()
    # Keep under default lease TTL (30s) so clock.advance for tick does not expire the lease.
    dl = now + timedelta(seconds=20)
    # Pre-reserve journal: task deadlines equal study.deadline; no finalize_reserve_s.
    study_payload = {
        "study_id": sid,
        "profile": "research",
        "request": "legacy",
        "catalog_version": 4,
        "hitl_policy": HitlPolicy().model_dump(mode="json"),
        "deadline": dl.isoformat(),
        "created_at": now.isoformat(),
        "parent_study_id": None,
        "budget": None,
        "require_hypotheses": False,
        "confirmations_needed": 1,
        "refutations_needed": 1,
        "refutation_priority": False,
        "supported_needed": 2,
        "max_expansions": 2,
        "max_attempts": 3,
    }
    assert "finalize_reserve_s" not in study_payload
    spec = TaskSpec(task_key="SPEC", title="SPEC", goal="SPEC", route="spec")
    spec_dump = spec.model_dump(mode="json")
    # Non-empty freeze — v14 upcast refuses empty {} on legacy journals.
    spec_dump["sandbox_check_limits"] = {
        "exec_ok": {
            "timeout_s": 5.0,
            "memory_mb": 64,
            "max_output_bytes": 1024,
            "pids_max": 16,
            "rlimit_nproc": 16,
            "reap_timeout_s": 1.0,
            "output_drain_join_s": 1.0,
            "allow_network": False,
            "allow_cgroup_degraded": False,
        }
    }
    task_payload = {
        "task_id": f"{sid}/SPEC",
        "kind": TaskKind.SPEC.value,
        "spec": spec_dump,
        "state": TaskState.PENDING.value,
        "outcome": None,
        "reason": None,
        "deadline": dl.isoformat(),
        "attempts": [],
        "inherited_from": None,
    }
    await seed_legacy_journal(
        store,
        [
            EventEnvelope(
                study_id=sid,
                type="StudyCreated",
                entity_key=sid,
                idem_key=make_idem("StudyCreated", sid),
                payload=study_payload,
                ts=now,
                schema_version=1,
            ),
            EventEnvelope(
                study_id=sid,
                type="TaskCreated",
                entity_key=f"{sid}/SPEC",
                idem_key=make_idem("TaskCreated", f"{sid}/SPEC"),
                payload=task_payload,
                ts=now,
                schema_version=1,
            ),
        ],
    )
    await store.release_lease("seeder", store.epoch)
    await store.close()

    rt = await open_runtime(tmp_path, sid, clock=clock, runtime_cls=runtime_cls)
    assert rt.study is not None
    assert rt.study.finalize_reserve_s == 0
    assert rt.study.effective_deadline() == rt.study.deadline
    task = rt.tasks[f"{sid}/SPEC"]
    assert task.deadline == rt.study.deadline
    assert rt.invariant_deadlines()

    await rt.ensure_lease(ttl_s=300)
    clock.advance(timedelta(seconds=21))
    await rt.tick()
    done = rt.tasks[f"{sid}/SPEC"]
    assert done.state == TaskState.DONE
    # PR2: now ≥ effective_deadline → study_deadline before P5 (even when
    # task.deadline == effective_deadline / study.deadline with reserve=0).
    assert done.reason == TaskReason.STUDY_DEADLINE
    assert done.outcome == TaskOutcome.SKIPPED
    assert rt.view().phase == "closed"
    await close_runtime(rt)
