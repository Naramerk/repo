"""M1 PR5: simulate=forbid default, H-c tolerate_failed_dep, method=simulation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coscientist.core.domain import (
    InputSpec,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.inputs import InputUnavailable, bind_inputs
from coscientist.core.reasons import TaskReason
from coscientist.core.runtime_factory import close_runtime, open_runtime


def _task(key: str, outcome: TaskOutcome | None, *, inputs=None, **spec_kw):
    return Task(
        task_id=f"st/{key}",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key=key,
            title=key,
            goal="g",
            route="experiment",
            inputs=inputs or [],
            **spec_kw,
        ),
        state=TaskState.DONE if outcome else TaskState.PENDING,
        outcome=outcome,
        deadline=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_default_forbid_no_stubs():
    """InputSpec.mode defaults to forbid — missing/failed dep raises."""
    assert InputSpec(from_task="A", output="main").mode == "forbid"
    dep = _task("A", TaskOutcome.FAILED)
    child = _task("B", None, inputs=[InputSpec(from_task="A", output="main")])
    child = child.model_copy(update={"state": TaskState.PENDING, "outcome": None})
    with pytest.raises(InputUnavailable):
        bind_inputs(
            child,
            dep_tasks={"A": dep},
            dep_outputs={"A": {}},
            dep_provenance={"A": "real"},
        )


@pytest.mark.asyncio
async def test_depends_on_failed_parent_skipped(tmp_path, clock):
    """H-c: depends_on FAILED parent without InputSpec → SKIPPED(input_unavailable)."""
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
    assert child.reason == TaskReason.INPUT_UNAVAILABLE
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_tolerate_failed_dep(tmp_path, clock):
    """tolerate_failed_dep=True allows starting after FAILED parent."""
    sid = "st_tol"
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
                task_key="C",
                title="c",
                goal="g",
                route="experiment",
                depends_on=["P"],
                tolerate_failed_dep=True,
            ),
        ],
        questions=[],
    )
    await rt.fail_attempt(await rt.start_attempt("P"), retryable=False)
    await rt.tick()
    child = rt.tasks[f"{sid}/C"]
    assert child.state != TaskState.DONE
    aid = await rt.start_attempt("C")
    assert aid
    assert rt.attempts[aid]["provenance"] == "real"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_method_simulation_real_provenance(tmp_path, clock):
    """experiment.method=simulation → attempt provenance=real (not stub)."""
    sid = "st_sim"
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
                task_key="E1",
                title="sim",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                method="simulation",
            ),
        ],
        questions=[],
    )
    aid = await rt.start_attempt("E1")
    assert rt.attempts[aid]["provenance"] == "real"
    await close_runtime(rt)


def test_graph_schema_allows_simulation_method():
    from coscientist.core.graph import load_schema

    schema = load_schema()
    exp = next(n for n in schema["nodes"] if n["kind"] == "experiment")
    method = next(f for f in exp["fields"] if f["name"] == "method")
    assert "simulation" in method["type"]
