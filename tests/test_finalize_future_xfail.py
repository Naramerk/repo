"""PR 3 strict xfail stubs that call real APIs (must be able to XPASS later)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from coscientist.core.domain import (
    Budget,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.runtime_factory import close_runtime, open_runtime


@pytest.mark.xfail(strict=True, reason="PR 4: budget=0 study refused at intake")
@pytest.mark.asyncio
async def test_budget_zero_study_refused(tmp_path, clock):
    rt = await open_runtime(tmp_path, "st_b0", clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=60),
        sid="st_b0",
        finalize_reserve_s=5,
        budget=Budget(max_cost_rub=Decimal("0")),
    )
    await rt.tick()
    fins = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert fins and fins[0].payload["trigger"] == "intake"
    assert fins[0].payload["terminal_reason"] == "refused"
    await close_runtime(rt)


@pytest.mark.xfail(strict=True, reason="PR 5: failed parent without InputSpec skips child")
@pytest.mark.asyncio
async def test_failed_parent_without_inputspec_skips_child(tmp_path, clock):
    from coscientist.core.reasons import TaskReason as TR

    sid = "st_fp"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="P", title="p", goal="g", route="experiment", depends_on=["PLAN"]
            ),
            TaskSpec(
                task_key="C", title="c", goal="g", route="experiment", depends_on=["P"]
            ),
        ],
        questions=[],
    )
    await rt.fail_attempt(await rt.start_attempt("P"), retryable=False)
    await rt.tick()
    child = rt.tasks[f"{sid}/C"]
    assert child.state == TaskState.DONE
    assert child.outcome == TaskOutcome.SKIPPED
    assert child.reason == TR.INPUT_UNAVAILABLE
    await close_runtime(rt)


@pytest.mark.xfail(strict=True, reason="PR 6/8: based_on required for counted dispositions")
def test_based_on_required_for_counted_dispositions():
    from coscientist.core.finalize_nodes import graph_rows_from_finalized

    _nodes, edges = graph_rows_from_finalized(
        "st_bo",
        {
            "question_dispositions": {
                "Q1": {"disposition": "answered", "reason": None, "formulation": "x"},
            },
            "terminal_reason": "completed",
        },
        created_at="2026-01-01T00:00:00+00:00",
    )
    based = [e for e in edges.values() if e.get("kind") == "based_on"]
    assert based, "answered conclusion must carry ≥1 based_on"


@pytest.mark.xfail(strict=True, reason="PR 6/8: question disposition from verdict()")
def test_question_disposition_computed_from_verdict():
    from coscientist.core.dispositions import dispositions

    t = Task(
        task_id="st/E1",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key="E1",
            title="e",
            goal="g",
            route="experiment",
            addresses=["Q1"],
        ),
        state=TaskState.DONE,
        outcome=TaskOutcome.SUCCESS,
        deadline=datetime(2026, 1, 1, tzinfo=UTC),
    )
    plan = {
        "plan_id": "pl",
        "questions": [{"id": "Q1", "text": "why"}],
        "tasks": [{"task_key": "E1", "addresses": ["Q1"]}],
    }
    d = dispositions(tasks={"st/E1": t}, plans={"pl": plan})
    assert d["Q1"]["disposition"] == "answered"


@pytest.mark.xfail(strict=True, reason="PR 8: explicit QuestionDisposed infeasible")
def test_question_disposition_explicit_infeasible():
    from coscientist.core.dispositions import dispositions

    plan = {
        "plan_id": "pl",
        "questions": [{"id": "Q1", "text": "why"}],
        "tasks": [],
    }
    d = dispositions(tasks={}, plans={"pl": plan})
    assert d["Q1"]["disposition"] == "infeasible"


@pytest.mark.xfail(
    strict=True,
    reason="PR 8: all_done→infeasible; PR2 only human_timeout|completed",
)
def test_terminal_reason_trigger_all_done_infeasible():
    from coscientist.core.finalize import terminal_reason_for

    tr = terminal_reason_for(
        "all_done",
        tasks={},
        question_dispositions={"Q1": {"disposition": "infeasible", "reason": "x"}},
    )
    assert tr.value == "infeasible"
