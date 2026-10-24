"""compute_outcome + B1/B2 / article_small tests (DESIGN §4.2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.errors import AttemptClosedError, CatalogError
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    CheckResult,
    DowngradeClaim,
    Outcome,
    ReqStatus,
    RequirementRef,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.execution.executor import Executor
from coscientist.core.execution.machine import new_owner
from coscientist.core.execution.outcome import compute_outcome
from coscientist.core.finalize.study_view import study_view


def _req(status: ReqStatus, *, code: bool = True, required: bool = True, i: int = 0):
    return CheckResult(
        role="validity",
        check_id=f"c{i}",
        template_id="t",
        status=status,
        is_code=code,
        required=required,
    )


def test_empty_required_never_success():
    with pytest.raises(CatalogError):
        compute_outcome([], None, None, "real")
    with pytest.raises(CatalogError):
        compute_outcome([_req(ReqStatus.MET, required=False)], None, None, "real")


def test_b2b_article_small_unassessed_review_does_not_downgrade():
    out, dg = compute_outcome(
        [_req(ReqStatus.MET, code=True, i=0), _req(ReqStatus.UNASSESSED, code=False, i=1)],
        None,
        None,
        "real",
    )
    assert out == Outcome.SUCCESS and dg is None


def test_b2a_article_small_claim_partial_without_reason_stays_success():
    out, dg = compute_outcome(
        [_req(ReqStatus.MET, code=True)], Outcome.PARTIAL, None, "real"
    )
    assert out == Outcome.SUCCESS and dg is None


def test_b2a_article_small_downgrade_with_reason_stored():
    out, dg = compute_outcome(
        [_req(ReqStatus.MET, code=True)], Outcome.PARTIAL, "agent unsure", "real"
    )
    assert out == Outcome.PARTIAL and dg is not None


def test_agent_claim_only_downgrades():
    out, _ = compute_outcome(
        [_req(ReqStatus.UNASSESSED, code=False)], Outcome.SUCCESS, "x", "real"
    )
    assert out == Outcome.PARTIAL
    out2, dg2 = compute_outcome(
        [_req(ReqStatus.MET, code=True)], Outcome.FAILED, "bad", "real"
    )
    assert out2 == Outcome.FAILED and dg2 is not None


def test_simulated_input_caps_at_partial():
    reqs = [_req(ReqStatus.MET, code=True)]
    assert compute_outcome(reqs, None, None, "stub")[0] == Outcome.PARTIAL
    assert compute_outcome(reqs, None, None, "partial")[0] == Outcome.PARTIAL


def test_b1_closed_only_when_all_tasks_done():
    """closed ⇔ finalized (StudyFinalized); all DONE alone → finalizing, not closed."""
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
    assert study_view(tasks).phase == "running"
    assert study_view([]).phase == "running"
    tasks[1] = tasks[1].model_copy(
        update={"state": TaskState.DONE, "outcome": TaskOutcome.SUCCESS}
    )
    view = study_view(tasks)
    assert view.phase == "finalizing"
    assert view.verdict == "fulfilled"
    closed = study_view(tasks, finalized=True)
    assert closed.phase == "closed"
    assert closed.verdict == "fulfilled"


async def _writer(tmp_path, clock, sid="st_b2"):
    store = SqliteEventStore(sid, tmp_path / "e.db", clock=clock)
    await store.acquire_lease(new_owner())
    writer = StudyWriter(store)
    writer.start()
    return store, writer


async def _seed_open_attempt(writer: StudyWriter, attempt: object, clock: FakeClock) -> None:
    """Projection must contain an open attempt — record checks closed inside the write txn."""
    aid = attempt.attempt_id  # type: ignore[attr-defined]
    await writer.append(
        UnstampedEvent(
            study_id=attempt.study_id,  # type: ignore[attr-defined]
            type="AttemptStarted",
            entity_key=aid,
            idem_key=make_idem("AttemptStarted", aid),
            payload={
                "attempt_id": aid,
                "n": 1,
                "dispatch_id": f"dispatch:{aid}",
                "route_returned": False,
                "closed": False,
                "close_reason": None,
                "provenance": attempt.provenance,  # type: ignore[attr-defined]
                "agent_output_digest": None,
                "deadline": clock.now().isoformat(),
                "task_id": attempt.task_id,  # type: ignore[attr-defined]
                "study_id": attempt.study_id,  # type: ignore[attr-defined]
            },
            ts=clock.now(),
        )
    )


@pytest.mark.asyncio
async def test_b2_matching_value_in_raw_artifact_is_success(tmp_path, catalog):
    clock = FakeClock()
    store, writer = await _writer(tmp_path, clock)
    arts = LocalArtifactStore(tmp_path / "a")
    art = arts.put(
        json.dumps({"count": 225}).encode(),
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
    attempt = type(
        "Att",
        (),
        {
            "attempt_id": "st_b2/EXP-1#1",
            "closed": False,
            "provenance": "real",
            "task_id": task.task_id,
            "study_id": "st_b2",
        },
    )()
    await _seed_open_attempt(writer, attempt, clock)
    ex = Executor(
        writer,
        catalog,
        arts,
        clock=clock,
        get_attempt=lambda _: attempt,
        get_task=lambda _: task,
    )
    result = await ex.record(
        "st_b2/EXP-1#1",
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 225},
        ),
    )
    assert result.outcome == Outcome.SUCCESS
    await writer.stop()
    await store.close()


@pytest.mark.asyncio
async def test_review_scheduled_on_every_record_path(tmp_path, catalog):
    clock = FakeClock()
    store, writer = await _writer(tmp_path, clock, "st_r")
    arts = LocalArtifactStore(tmp_path / "a")
    art = arts.put(
        b'{"count":225}',
        make_tool_ref("st_r/EXP-1#1:1"),
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
    attempt = type(
        "Att",
        (),
        {
            "attempt_id": "st_r/EXP-1#1",
            "closed": False,
            "provenance": "real",
            "task_id": task.task_id,
            "study_id": "st_r",
        },
    )()
    await _seed_open_attempt(writer, attempt, clock)
    ex = Executor(
        writer,
        catalog,
        arts,
        clock=clock,
        get_attempt=lambda _: attempt,
        get_task=lambda _: task,
        review_statuses={"human_review#1": ReqStatus.UNASSESSED},
    )
    result = await ex.record(
        "st_r/EXP-1#1",
        AgentTaskOutput(
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")}
        ),
    )
    assert any(e.type == "ReviewScheduled" for e in await store.read_all())
    assert result.outcome == Outcome.SUCCESS
    await writer.stop()
    await store.close()


@pytest.mark.asyncio
async def test_closed_attempt_rejects_record(tmp_path, catalog):
    clock = FakeClock()
    store, writer = await _writer(tmp_path, clock, "st_c")
    arts = LocalArtifactStore(tmp_path / "a")
    attempt = type(
        "Att",
        (),
        {
            "attempt_id": "a#1",
            "closed": True,
            "provenance": "real",
            "task_id": "t",
            "study_id": "st_c",
        },
    )()
    ex = Executor(
        writer,
        catalog,
        arts,
        clock=clock,
        get_attempt=lambda _: attempt,
        get_task=lambda _: None,
    )
    with pytest.raises(AttemptClosedError):
        await ex.record("a#1", AgentTaskOutput(summary="late"))
    assert any(e.type == "LateWriteRejected" for e in await store.read_all())
    await writer.stop()
    await store.close()


@pytest.mark.asyncio
async def test_downgrade_without_refs_rejected(tmp_path, catalog):
    clock = FakeClock()
    store, writer = await _writer(tmp_path, clock, "st_d")
    arts = LocalArtifactStore(tmp_path / "a")
    art = arts.put(
        b'{"count":225}',
        make_tool_ref("st_d/EXP-1#1:1"),
        name="main",
        media_type="application/json",
    )
    spec = TaskSpec(
        task_key="EXP-1",
        title="t",
        goal="g",
        route="experiment",
        criteria=[RequirementRef(template_id="value_match")],
    )
    task = Task(
        task_id="st_d/EXP-1",
        kind=TaskKind.EXPERIMENT,
        spec=spec,
        state=TaskState.RUNNING,
        deadline=clock.now() + timedelta(hours=1),
        attempts=["st_d/EXP-1#1"],
    )
    attempt = type(
        "Att",
        (),
        {
            "attempt_id": "st_d/EXP-1#1",
            "closed": False,
            "provenance": "real",
            "task_id": task.task_id,
            "study_id": "st_d",
        },
    )()
    await _seed_open_attempt(writer, attempt, clock)
    ex = Executor(
        writer, catalog, arts, clock=clock, get_attempt=lambda _: attempt, get_task=lambda _: task
    )
    result = await ex.record(
        "st_d/EXP-1#1",
        AgentTaskOutput(
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            downgrade=DowngradeClaim(to=Outcome.PARTIAL, refs=[], reason="no refs"),
        ),
    )
    assert any(e.type == "DowngradeRejected" for e in await store.read_all())
    assert result.outcome == Outcome.SUCCESS
    await writer.stop()
    await store.close()
