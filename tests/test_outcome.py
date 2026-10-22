"""compute_outcome + B1/B2 / article_small tests (DESIGN §4.2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.clock import FakeClock
from coscientist.core.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    CheckResult,
    Outcome,
    ReqStatus,
    RequirementRef,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.errors import AttemptClosedError, CatalogError
from coscientist.core.executor import Executor
from coscientist.core.outcome import compute_outcome
from coscientist.core.store import SqliteEventStore
from coscientist.core.study_view import study_view
from coscientist.core.writer import StudyWriter


def _req(status: ReqStatus, *, code: bool = True, required: bool = True, i: int = 0):
    return CheckResult(
        check_id=f"c{i}",
        template_id="t",
        status=status,
        is_code=code,
        required=required,
    )


def test_empty_required_never_success():
    with pytest.raises(CatalogError):
        compute_outcome([], None, None, False)
    with pytest.raises(CatalogError):
        compute_outcome([_req(ReqStatus.MET, required=False)], None, None, False)


def test_b2b_article_small_unassessed_review_does_not_downgrade():
    reqs = [
        _req(ReqStatus.MET, code=True, i=0),
        _req(ReqStatus.UNASSESSED, code=False, i=1),
    ]
    out, dg = compute_outcome(reqs, None, None, False)
    assert out == Outcome.SUCCESS
    assert dg is None


def test_b2a_article_small_claim_partial_without_reason_stays_success():
    reqs = [_req(ReqStatus.MET, code=True)]
    out, dg = compute_outcome(reqs, Outcome.PARTIAL, None, False)
    assert out == Outcome.SUCCESS
    assert dg is None


def test_b2a_article_small_downgrade_with_reason_stored():
    reqs = [_req(ReqStatus.MET, code=True)]
    out, dg = compute_outcome(reqs, Outcome.PARTIAL, "agent unsure", False)
    assert out == Outcome.PARTIAL
    assert dg is not None
    assert dg.from_ == Outcome.SUCCESS
    assert dg.to == Outcome.PARTIAL


def test_agent_claim_only_downgrades():
    only_review = [_req(ReqStatus.UNASSESSED, code=False)]
    out, _ = compute_outcome(only_review, Outcome.SUCCESS, "x", False)
    assert out == Outcome.PARTIAL

    reqs = [_req(ReqStatus.MET, code=True)]
    out2, dg2 = compute_outcome(reqs, Outcome.FAILED, "bad", False)
    assert out2 == Outcome.FAILED and dg2 is not None


def test_simulated_input_caps_at_partial():
    reqs = [_req(ReqStatus.MET, code=True)]
    out, _ = compute_outcome(reqs, None, None, True)
    assert out == Outcome.PARTIAL


def test_b1_closed_only_when_all_tasks_done():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    spec = TaskSpec(task_key="A", title="a", goal="g", route="experiment")
    tasks = [
        Task(
            task_id="st/A",
            kind=TaskKind.EXPERIMENT,
            spec=spec,
            state=TaskState.DONE,
            outcome=TaskOutcome.SUCCESS,
            deadline=now,
        ),
        Task(
            task_id="st/B",
            kind=TaskKind.EXPERIMENT,
            spec=spec.model_copy(update={"task_key": "B"}),
            state=TaskState.RUNNING,
            deadline=now,
        ),
    ]
    view = study_view(tasks)
    assert view.phase == "running"
    assert view.verdict is None

    tasks[1] = tasks[1].model_copy(
        update={"state": TaskState.DONE, "outcome": TaskOutcome.SUCCESS}
    )
    view2 = study_view(tasks)
    assert view2.phase == "closed"
    assert view2.verdict == "fulfilled"


def _attempt_obj(d: dict):
    return type("Att", (), d)()


@pytest.mark.asyncio
async def test_b2_matching_value_in_raw_artifact_is_success(tmp_path, catalog):
    clock = FakeClock()
    store = SqliteEventStore("st_b2", tmp_path / "e.db", clock=clock)
    await store.acquire_lease("t")
    writer = StudyWriter(store)
    arts = LocalArtifactStore(tmp_path / "a")
    content = json.dumps({"count": 225}).encode()
    art = arts.put(
        content,
        make_tool_ref("st_b2/EXP-1#1:1"),
        name="main",
        media_type="application/json",
    )
    spec = TaskSpec(
        task_key="EXP-1",
        title="article_small",
        goal="count",
        route="experiment",
        criteria=[RequirementRef(template_id="value_match")],
    )
    task = Task(
        task_id="st_b2/EXP-1",
        kind=TaskKind.EXPERIMENT,
        spec=spec,
        state=TaskState.RUNNING,
        deadline=clock.now() + timedelta(hours=1),
        attempts=["st_b2/EXP-1#1"],
    )
    attempt = _attempt_obj(
        {
            "attempt_id": "st_b2/EXP-1#1",
            "closed": False,
            "simulated": False,
            "task_id": task.task_id,
            "study_id": "st_b2",
        }
    )
    ex = Executor(
        writer,
        catalog,
        arts,
        clock=clock,
        get_attempt=lambda _: attempt,
        get_task=lambda _: task,
    )
    result = ex.record(
        "st_b2/EXP-1#1",
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 225},
        ),
    )
    assert result.outcome == Outcome.SUCCESS
    store.close()


@pytest.mark.asyncio
async def test_review_scheduled_on_every_record_path(tmp_path, catalog):
    clock = FakeClock()
    store = SqliteEventStore("st_r", tmp_path / "e.db", clock=clock)
    await store.acquire_lease("t")
    writer = StudyWriter(store)
    arts = LocalArtifactStore(tmp_path / "a")
    art = arts.put(
        b'{"count":225}',
        make_tool_ref("x:1"),
        name="main",
        media_type="application/json",
    )
    spec = TaskSpec(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        criteria=[
            RequirementRef(template_id="value_match"),
            RequirementRef(template_id="human_review"),
        ],
    )
    task = Task(
        task_id="st_r/EXP-1",
        kind=TaskKind.EXPERIMENT,
        spec=spec,
        state=TaskState.RUNNING,
        deadline=clock.now() + timedelta(hours=1),
        attempts=["st_r/EXP-1#1"],
    )
    attempt = _attempt_obj(
        {
            "attempt_id": "st_r/EXP-1#1",
            "closed": False,
            "simulated": False,
            "task_id": task.task_id,
            "study_id": "st_r",
        }
    )
    ex = Executor(
        writer,
        catalog,
        arts,
        clock=clock,
        get_attempt=lambda _: attempt,
        get_task=lambda _: task,
        review_statuses={"human_review#1": ReqStatus.UNASSESSED},
    )
    result = ex.record(
        "st_r/EXP-1#1",
        AgentTaskOutput(
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")}
        ),
    )
    assert any(e.type == "ReviewScheduled" for e in store.read_all())
    assert result.outcome == Outcome.SUCCESS
    store.close()


@pytest.mark.asyncio
async def test_closed_attempt_rejects_record(tmp_path, catalog):
    clock = FakeClock()
    store = SqliteEventStore("st_c", tmp_path / "e.db", clock=clock)
    await store.acquire_lease("t")
    writer = StudyWriter(store)
    arts = LocalArtifactStore(tmp_path / "a")
    attempt = _attempt_obj(
        {
            "attempt_id": "a#1",
            "closed": True,
            "simulated": False,
            "task_id": "t",
            "study_id": "st_c",
        }
    )
    ex = Executor(
        writer,
        catalog,
        arts,
        clock=clock,
        get_attempt=lambda _: attempt,
        get_task=lambda _: None,
    )
    with pytest.raises(AttemptClosedError):
        ex.record("a#1", AgentTaskOutput(summary="late"))
    assert any(e.type == "LateWriteRejected" for e in store.read_all())
    store.close()
