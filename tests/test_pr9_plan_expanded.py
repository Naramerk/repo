"""M1 PR9: PlanExpanded, quotas, replan_reserve, question supersede, k filter."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.checks.evaluate import evaluate_code_check
from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    CodeCheck,
    ExpansionRequest,
    QuestionSpec,
    QuestionSupersede,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.ids import question_node_id
from coscientist.core.reasons import TaskReason
from coscientist.core.supersede import question_is_superseded
from coscientist.core.upcasters import upcast
from tests.helpers import attach_executor, close_runtime, open_runtime


@pytest.mark.asyncio
async def test_expand_after_exp(tmp_path, clock, runtime_cls):
    """expand_after + expansion_request after EXP creates new tasks (H-g)."""
    sid = "st_exp_after"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=3,
    )
    attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
        expand_after=["E0"],
    )
    aid = await rt.start_attempt("E0")
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="ok",
            expansion_request=ExpansionRequest(
                tasks=[
                    TaskSpec(
                        task_key="E1",
                        title="follow",
                        goal="g",
                        route="experiment",
                        depends_on=["E0"],
                        addresses=["Q1"],
                    )
                ],
                reason="need follow-up",
            ),
        ),
    )
    assert f"{sid}/E1" in rt.tasks
    assert rt._agent_expansions == 1
    events = await rt.store.read_all()
    assert any(e.type == "PlanExpanded" for e in events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_expansion_limit_reported(tmp_path, clock, runtime_cls):
    """Agent quota exhaustion → ExpansionRefused(expansion_limit), no silent drop."""
    sid = "st_exp_lim"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=1,
    )
    attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    await rt.start_attempt("E0")
    await rt.cancel_task("E0", reason="cancelled")
    pid = await rt.expand_plan(
        after_task="E0",
        added_tasks=[
            TaskSpec(
                task_key="E1",
                title="a",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                addresses=["Q1"],
            )
        ],
        origin="emergent",
        reason="first",
    )
    assert pid is not None
    refused = await rt.expand_plan(
        after_task="E1",
        added_tasks=[
            TaskSpec(
                task_key="E2",
                title="b",
                goal="g",
                route="experiment",
                depends_on=["E1"],
                addresses=["Q1"],
            )
        ],
        origin="emergent",
        reason="second",
    )
    assert refused is None
    assert any(r.get("reason") == "expansion_limit" for r in rt._expansion_refusals)
    assert f"{sid}/E2" not in rt.tasks
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_human_expand_does_not_consume_agent_quota(tmp_path, clock, runtime_cls):
    sid = "st_hum"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=0,
        max_human_expansions=2,
    )
    attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    # Agent expand refused (quota 0).
    assert (
        await rt.expand_plan(
            after_task="E0",
            added_tasks=[
                TaskSpec(
                    task_key="Ea",
                    title="a",
                    goal="g",
                    route="experiment",
                    depends_on=["E0"],
                    addresses=["Q1"],
                )
            ],
            origin="emergent",
        )
        is None
    )
    # Human «decide yourself» / plan edit uses human quota.
    pid = await rt.expand_plan(
        after_task="E0",
        added_tasks=[
            TaskSpec(
                task_key="Eh",
                title="h",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                addresses=["Q1"],
            )
        ],
        origin="plan",
        delegated_by="human",
        reason="hitl_delegate",
    )
    assert pid is not None
    assert rt._human_expansions == 1
    assert rt._agent_expansions == 0
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_emergent_question_added(tmp_path, clock, runtime_cls):
    sid = "st_eq"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
    )
    attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    await rt.expand_plan(
        after_task="E0",
        added_tasks=[
            TaskSpec(
                task_key="E1",
                title="gap",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                addresses=["Q2"],
            )
        ],
        added_questions=[QuestionSpec(id="Q2", text="gap?")],
        origin="emergent",
        reason="emergent_gap",
    )
    assert rt.graph.get_node(question_node_id(sid, "Q2")) is not None
    assert f"{sid}/E1" in rt.tasks
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_hitl_question_edit_skips_old_tasks(tmp_path, clock, runtime_cls):
    """HITL question edit = PlanExpanded; old-only tasks → SKIPPED(superseded)."""
    sid = "st_qedit"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
    )
    attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="old",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E1",
                title="pending",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                addresses=["Q1"],
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="old formulation")],
    )
    await rt.start_attempt("E0")
    await rt.cancel_task("E0", reason="cancelled")
    # E1 still PENDING — will be superseded with the question.
    await rt.expand_plan(
        after_task="E0",
        added_tasks=[
            TaskSpec(
                task_key="E2",
                title="new",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                addresses=["Q1b"],
            )
        ],
        added_questions=[QuestionSpec(id="Q1b", text="new formulation")],
        superseded_questions=[QuestionSupersede(new_id="Q1b", old_id="Q1")],
        origin="plan",
        delegated_by="human",
        reason="hitl_question_edit",
    )
    e1 = rt.tasks[f"{sid}/E1"]
    assert e1.state.value == "DONE"
    assert str(getattr(e1.reason, "value", e1.reason)) == TaskReason.SUPERSEDED.value
    assert question_is_superseded(rt.graph, sid, "Q1")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_superseded_question_claims_not_counted(tmp_path, clock, runtime_cls):
    """C10: claims about a superseded question are excluded from k."""
    from coscientist.core.claims import filtered_claims_by_target

    sid = "st_c10"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
        refutations_needed=1,
        refutation_priority=True,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="old")],
    )
    aid = await rt.start_attempt("E0")
    art = arts.put(
        b'{"count": 999}',
        make_tool_ref(f"{aid}:1"),
        name="main",
        media_type="application/json",
    )
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="x",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 999},
            claims=[{"id": "c1", "target": "Q1"}],
        ),
    )
    await rt.expand_plan(
        after_task="E0",
        added_tasks=[
            TaskSpec(
                task_key="E1",
                title="n",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                addresses=["Q2"],
            )
        ],
        added_questions=[QuestionSpec(id="Q2", text="new")],
        superseded_questions=[QuestionSupersede(new_id="Q2", old_id="Q1")],
        delegated_by="human",
        origin="plan",
    )
    by = filtered_claims_by_target(rt.graph, study_id=sid, results=rt.results)
    old_claims = by.get("question:Q1") or []
    assert old_claims
    assert all(c.exclusion_reason == "superseded" for c in old_claims)
    assert all(not c.counts() for c in old_claims)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_replan_reserve_refuses_when_exhausted(tmp_path, clock, runtime_cls):
    sid = "st_rr"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    # Tiny reserve: almost no expansion window.
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=40),
        sid=sid,
        finalize_reserve_s=5,
        replan_reserve=0.0,
        max_expansions=5,
    )
    attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="t",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    assert (
        await rt.expand_plan(
            after_task="E0",
            added_tasks=[
                TaskSpec(
                    task_key="E1",
                    title="a",
                    goal="g",
                    route="experiment",
                    depends_on=["E0"],
                    addresses=["Q1"],
                )
            ],
            origin="emergent",
        )
        is None
    )
    assert any(r.get("detail") == "replan_reserve" for r in rt._expansion_refusals)
    await close_runtime(rt)


def test_upcaster_studycreated_v6_to_v7_expansion_fields():
    v, p = upcast("StudyCreated", 6, {"study_id": "s", "max_replans": 4})
    assert v >= 7
    assert p["max_expansions"] == 4
    assert p["max_human_expansions"] == 10
    assert p["replan_reserve"] == 0.1


def test_upcaster_planaccepted_v3_to_v4_expand_after():
    v, p = upcast("PlanAccepted", 3, {"plan_id": "p", "tasks": [], "questions": []})
    assert v >= 4
    assert p["expand_after"] == []


def test_schema_valid_missing_artifact(tmp_path):
    """#15 follow-up: schema_valid with missing artifact → artifact_missing."""
    from coscientist.core.domain import ReqStatus

    arts = LocalArtifactStore(tmp_path / "arts")
    check = CodeCheck(kind="schema_valid", output="main")
    out = AgentTaskOutput(outputs={"main": ArtifactHandle(artifact_id="art_missing_xyz", name="main")})
    cr = evaluate_code_check(
        check,
        check_id="c1",
        template_id="schema_ok",
        out=out,
        values={},
        artifacts=arts,
        role="validity",
        attempt_id="st/E0#1",
    )
    assert cr.status == ReqStatus.FAILED
    assert cr.detail == "artifact_missing"
    assert cr.effect is None
