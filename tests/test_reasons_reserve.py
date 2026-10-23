"""PR 1: TaskReason/CloseReason, provenance stub, study_view, finalize reserve."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coscientist.core.domain import (
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.reasons import (
    CloseReason,
    Disposition,
    NotAttemptedReason,
    RefusalKind,
    TaskReason,
    TerminalReason,
    intake,
)
from coscientist.core.runtime_factory import close_runtime, open_runtime
from coscientist.core.study_view import study_view
from coscientist.core.upcasters import upcast

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


def test_task_reason_enum_roundtrip():
    for s in _TASK_REASON_STRINGS:
        assert TaskReason(s).value == s
    for s in _CLOSE_REASON_STRINGS:
        assert CloseReason(s).value == s
    # Declared-only enums for later PRs
    assert TerminalReason.COMPLETED.value == "completed"
    assert Disposition.NOT_ATTEMPTED.value == "not_attempted"
    assert NotAttemptedReason.NO_TASK.value == "no_task"
    assert RefusalKind.BUDGET_ZERO.value == "budget_zero"
    with pytest.raises(NotImplementedError, match="PR 4"):
        intake("", [], "p", None)


@pytest.mark.parametrize("reason", _TASK_REASON_STRINGS)
def test_upcast_every_legacy_reason(reason: str):
    v, payload = upcast("TaskDone", 1, {"task_id": "t", "outcome": "FAILED", "reason": reason})
    assert v == 2
    assert payload["reason"] == reason
    if reason in _CLOSE_REASON_STRINGS:
        v2, p2 = upcast(
            "AttemptClosed",
            1,
            {"attempt_id": "a", "closed": True, "close_reason": reason},
        )
        assert v2 == 2
        assert p2["close_reason"] == reason


def test_upcast_unknown_reason_raises():
    with pytest.raises(ValueError, match="unknown TaskDone.reason"):
        upcast("TaskDone", 1, {"task_id": "t", "outcome": "FAILED", "reason": "nope"})
    with pytest.raises(ValueError, match="unknown AttemptClosed.close_reason"):
        upcast(
            "AttemptClosed",
            1,
            {"attempt_id": "a", "closed": True, "close_reason": "nope"},
        )


@pytest.mark.asyncio
async def test_writer_emits_v2(tmp_path: Path, clock, runtime_cls):
    rt = await open_runtime(tmp_path, "st_v2", clock=clock, runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_v2",
        finalize_reserve_s=5,
    )
    await rt.cancel_task("SPEC", reason=TaskReason.CANCELLED)
    events = await rt.store.read_all()
    done = [e for e in events if e.type == "TaskDone"]
    closed = [e for e in events if e.type == "AttemptClosed"]
    assert done
    assert all(e.schema_version == 2 for e in done)
    assert all(e.payload.get("reason") in (None, "cancelled") for e in done)
    # SPEC had no open attempt — cancel may not emit AttemptClosed
    assert all(e.schema_version == 2 for e in closed)
    await close_runtime(rt)


def test_upcast_provenance_simulated_to_stub():
    v, p = upcast("AttemptStarted", 1, {"provenance": "simulated", "attempt_id": "a"})
    assert v == 2 and p["provenance"] == "stub"
    v, p = upcast(
        "ResultRecorded",
        1,
        {
            "provenance": "simulated",
            "inputs": [{"input": "x", "artifact_id": "a", "source": "simulated"}],
            "verdict": "simulated",
        },
    )
    assert v == 2
    assert p["provenance"] == "stub"
    assert p["inputs"][0]["source"] == "stub"
    assert p["verdict"] == "stubbed"


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


def test_study_view_stubbed_computed():
    # required SUCCESS + one required stub → stubbed (no simulated_done arg)
    req_real = _task("A", attempts=["a1"])
    req_stub = _task("B", outcome=TaskOutcome.PARTIAL, attempts=["a2"])
    atts = {
        "a1": {"provenance": "real", "closed": True},
        "a2": {"provenance": "stub", "closed": True},
    }
    view = study_view([req_real, req_stub], attempts=atts)
    assert view.verdict == "stubbed"
    assert view.stub_share == 0.5

    # only non-required stub → fulfilled
    req = _task("R", attempts=["r1"])
    opt = _task("O", required=False, attempts=["o1"])
    atts2 = {
        "r1": {"provenance": "real", "closed": True},
        "o1": {"provenance": "stub", "closed": True},
    }
    view2 = study_view([req, opt], attempts=atts2)
    assert view2.verdict == "fulfilled"
    assert view2.stub_share == 0.5


@pytest.mark.asyncio
async def test_create_study_rejects_duration_le_reserve(tmp_path: Path, clock, runtime_cls):
    rt = await open_runtime(tmp_path, "st_rej", clock=clock, runtime_cls=runtime_cls)
    with pytest.raises(ValueError, match="finalize_reserve_s"):
        await rt.create_study(
            profile="research",
            request="q",
            catalog_version="0.1.0",
            deadline=clock.now() + timedelta(seconds=30),
            sid="st_rej",
            finalize_reserve_s=30,
        )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_all_deadlines_le_effective(tmp_path: Path, clock, runtime_cls):
    rt = await open_runtime(tmp_path, "st_eff", clock=clock, runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=10),
        sid="st_eff",
        finalize_reserve_s=30,
    )
    assert rt.study is not None
    ed = rt.study.effective_deadline()
    assert ed == rt.study.deadline - timedelta(seconds=30)
    for t in rt.tasks.values():
        assert t.deadline <= ed
    assert rt.invariant_deadlines()
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_plan_deadline_clipped_by_reserve(tmp_path: Path, clock, runtime_cls):
    rt = await open_runtime(tmp_path, "st_plan", clock=clock, runtime_cls=runtime_cls)
    # Study shorter than 600s plan window → plan clipped to effective_deadline
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=120),
        sid="st_plan",
        finalize_reserve_s=30,
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
    assert v == 2 and p["reason"] is None
