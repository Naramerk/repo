"""M1 PR5: simulate=forbid default, H-c tolerate_failed_dep, method=simulation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coscientist.core.domain import (
    ExperimentMethod,
    InputSpec,
    OutputSpec,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.inputs import InputUnavailable, bind_inputs
from coscientist.core.reasons import TaskReason
from coscientist.core.upcasters import upcast
from tests.helpers import attach_executor, close_runtime, open_runtime, record_success


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


def test_upcast_input_mode_keeps_old_default():
    """TaskCreated v3 without mode → use_partial (pre-PR5 default), not forbid."""
    v, p = upcast(
        "TaskCreated",
        3,
        {
            "task_id": "t",
            "spec": {
                "task_key": "E1",
                "inputs": [{"from_task": "A", "output": "main"}],
            },
        },
    )
    assert v == 6
    assert p["spec"]["inputs"][0]["mode"] == "use_partial"
    assert p["spec"]["inputs"][0]["kind"] == "artifact"


def test_experiment_method_enum_matches_graph():
    from coscientist.core.graph import load_schema

    schema = load_schema()
    exp = next(n for n in schema["nodes"] if n["kind"] == "experiment")
    method = next(f for f in exp["fields"] if f["name"] == "method")
    schema_vals = set(method["type"].removeprefix("enum(").removesuffix(")").split("|"))
    assert {m.value for m in ExperimentMethod} == schema_vals
    assert "simulation" in schema_vals
    assert method.get("required") is False  # aligns with TaskSpec.method Optional


@pytest.mark.asyncio
async def test_depends_on_failed_parent_skipped(tmp_path, clock, runtime_cls):
    """H-c: depends_on FAILED parent without InputSpec → SKIPPED(input_unavailable)."""
    sid = "st_fp"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
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
async def test_cancel_task_cascades_via_hc(tmp_path, clock, runtime_cls):
    """cancel_task(parent) → tick H-c skips dependents with input_unavailable."""
    sid = "st_casc"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
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
    await rt.start_attempt("P")
    await rt.cancel_task("P", reason="cancelled")
    assert rt.tasks[f"{sid}/C"].state != TaskState.DONE
    await rt.tick()
    child = rt.tasks[f"{sid}/C"]
    assert child.state == TaskState.DONE
    assert child.outcome == TaskOutcome.SKIPPED
    assert child.reason == TaskReason.INPUT_UNAVAILABLE
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_hc_skipped_when_budget_exceeded(tmp_path, clock, runtime_cls):
    """record_budget_debit → tick: H-c must not rewrite children to input_unavailable."""
    from decimal import Decimal

    from coscientist.core.domain import Budget

    sid = "st_hc_bud"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        budget=Budget(max_cost_rub=Decimal("25")),
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
    await rt.record_budget_debit(spent_rub=Decimal("25"), request_id="debit-hc")
    await rt.tick()
    child = rt.tasks[f"{sid}/C"]
    assert child.state == TaskState.DONE
    assert child.reason == TaskReason.BUDGET
    fins = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fins) == 1 and fins[0].payload["trigger"] == "budget"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_hc_skipped_when_study_cancelled(tmp_path, clock, runtime_cls):
    """StudyCancelled without closing deps → tick skips H-c; children get cancelled."""
    from coscientist.core.events import EventEnvelope, make_idem

    sid = "st_hc_cx"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
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
    await rt._append(
        [
            EventEnvelope(
                study_id=sid,
                type="StudyCancelled",
                entity_key=sid,
                idem_key=make_idem("StudyCancelled", sid),
                payload={"study_id": sid, "by": "user"},
                ts=clock.now(),
            )
        ]
    )
    await rt.tick()
    child = rt.tasks[f"{sid}/C"]
    assert child.state == TaskState.DONE
    assert child.reason == TaskReason.CANCELLED
    fins = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fins) == 1 and fins[0].payload["trigger"] == "cancel"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_tolerate_failed_dep(tmp_path, clock, runtime_cls):
    """tolerate_failed_dep=True allows starting after FAILED parent."""
    sid = "st_tol"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
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
async def test_failed_spec_plan_still_runs_and_report(tmp_path, clock, runtime_cls):
    """BLOCKER: SPEC FAILED must not H-c-cascade stages; PLAN runs; report exists."""
    sid = "st_spec_fail"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="raw request for plan",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
    )
    _, arts = attach_executor(rt, tmp_path)
    await rt.fail_attempt(await rt.start_attempt("SPEC"), retryable=False)
    await rt.tick()
    for key in ("HYP", "LIT", "PLAN", "REPORT"):
        t = rt.tasks[f"{sid}/{key}"]
        assert t.state != TaskState.DONE, f"stage {key} must not be H-c skipped"
    # Intermediate stages SKIPPED (DONE) so PLAN deps are satisfied; H-c must not wipe PLAN.
    for key in ("HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.tick()
    plan = rt.tasks[f"{sid}/PLAN"]
    assert plan.state == TaskState.PENDING
    await record_success(rt, await rt.start_attempt("PLAN"), arts)
    assert rt.tasks[f"{sid}/PLAN"].state == TaskState.DONE
    assert rt.tasks[f"{sid}/PLAN"].outcome == TaskOutcome.SUCCESS
    await record_success(rt, await rt.start_attempt("REPORT"), arts)
    assert rt.tasks[f"{sid}/REPORT"].state == TaskState.DONE
    assert rt.tasks[f"{sid}/REPORT"].outcome == TaskOutcome.SUCCESS
    # finalize reads artifacts via executor.artifacts (attach_executor).
    await rt.tick()
    fins = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fins) == 1
    assert fins[0].payload.get("report_artifact_id")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_method_simulation_real_provenance(tmp_path, clock, runtime_cls):
    """experiment.method=simulation with clean inputs → provenance=real."""
    sid = "st_sim"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
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
                method=ExperimentMethod.SIMULATION,
            ),
        ],
        questions=[],
    )
    aid = await rt.start_attempt("E1")
    assert rt.attempts[aid]["provenance"] == "real"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_method_simulation_keeps_stub_input_provenance(
    tmp_path, clock, runtime_cls
):
    """method=simulation must not upgrade derived=stub from inputs (H-b / §6.7)."""
    sid = "st_sim_stub"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
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
                task_key="P",
                title="p",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                expected_outputs=[
                    OutputSpec(name="main", media_type="application/json")
                ],
            ),
            TaskSpec(
                task_key="S",
                title="sim",
                goal="g",
                route="experiment",
                depends_on=["P"],
                method=ExperimentMethod.SIMULATION,
                inputs=[
                    InputSpec(from_task="P", output="main", mode="simulate"),
                ],
            ),
        ],
        questions=[],
    )
    await rt.fail_attempt(await rt.start_attempt("P"), retryable=False)
    aid = await rt.start_attempt("S")
    assert rt.attempts[aid]["provenance"] == "stub"
    await close_runtime(rt)


def test_graph_schema_allows_simulation_method():
    from coscientist.core.graph import load_schema

    schema = load_schema()
    exp = next(n for n in schema["nodes"] if n["kind"] == "experiment")
    method = next(f for f in exp["fields"] if f["name"] == "method")
    assert "simulation" in method["type"]
