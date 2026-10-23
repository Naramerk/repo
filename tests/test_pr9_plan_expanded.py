"""M1 PR9: PlanExpanded, quotas, replan_reserve, question supersede, k filter.

Review fixes: B1–B3, MJ1–MJ6, minors (PLAN-n path, report lines, fold, HITL).
"""

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
from coscientist.core.expand import apply_pending_expansion
from coscientist.core.ids import question_node_id
from coscientist.core.reasons import TaskReason
from coscientist.core.supersede import question_is_superseded
from coscientist.core.upcasters import upcast
from coscientist.core.verdict import recompute_effect_provenance
from tests.helpers import attach_executor, close_runtime, open_runtime


async def _boot_plan(rt, *, tasks, questions, expand_after=None, **study_kw):
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(tasks, questions=questions, expand_after=expand_after or [])


@pytest.mark.asyncio
async def test_expand_after_exp(tmp_path, clock, runtime_cls):
    """expansion_request → PLAN-n → critic apply → PlanExpanded (MJ2)."""
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
    await _boot_plan(
        rt,
        tasks=[
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
                        task_key="agent_wants_E1",
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
    # MJ2: PLAN-n container, not direct PlanExpanded.
    plan_n = [t for t in rt.tasks.values() if t.spec.task_key.startswith("PLAN-")]
    assert plan_n, "expected PLAN-n container for expansion_request"
    events = await rt.store.read_all()
    assert not any(e.type == "PlanExpanded" for e in events)
    # Critic accept → apply pending (core assigns EXP-<n>).
    pid = await apply_pending_expansion(rt, plan_n[0].spec.task_key)
    assert pid is not None
    assert rt._agent_expansions == 1
    events = await rt.store.read_all()
    assert any(e.type == "PlanExpanded" for e in events)
    exp_keys = [k for k in rt.tasks if "/EXP-" in k]
    assert exp_keys, "core must assign EXP-<n> keys"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_garbage_expansion_request_refused_study_finalizes(tmp_path, clock, runtime_cls):
    """B1: unknown dep → ExpansionRefused(invalid); never raise; study finalizes."""
    sid = "st_garbage"
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
    await _boot_plan(
        rt,
        tasks=[
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
    aid = await rt.start_attempt("E0")
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="bad",
            expansion_request=ExpansionRequest(
                tasks=[
                    TaskSpec(
                        task_key="E1",
                        title="x",
                        goal="g",
                        route="experiment",
                        depends_on=["NO_SUCH_TASK"],
                        addresses=["Q1"],
                    )
                ],
                reason="garbage",
            ),
        ),
    )
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "ExpansionRefused"]
    assert refused
    assert refused[0].payload.get("reason") == "invalid"
    assert not any(e.type == "PlanExpanded" for e in events)
    # Study still finalizes (B1).
    for key in list(rt.tasks):
        t = rt.tasks[key]
        if t.state.value != "DONE":
            await rt.cancel_task(t.spec.task_key, reason="cancelled")
    await rt.tick()
    # Force finalize via cancel if not yet.
    if not rt._finalized:
        await rt.cancel_study()
    events = await rt.store.read_all()
    fin = [e for e in events if e.type == "StudyFinalized"]
    assert fin
    contrib = fin[0].payload.get("contributing_reasons") or []
    assert any(str(c).startswith("not done:") for c in contrib)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_expansion_limit_reported(tmp_path, clock, runtime_cls):
    """Agent quota exhaustion → ExpansionRefused in StudyFinalized/report (B2)."""
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
    await _boot_plan(
        rt,
        tasks=[
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
    assert f"{sid}/E2" not in rt.tasks
    for key in list(rt.tasks):
        t = rt.tasks[key]
        if t.state.value != "DONE":
            await rt.cancel_task(t.spec.task_key, reason="cancelled")
    if not rt._finalized:
        await rt.cancel_study()
    events = await rt.store.read_all()
    fin = next(e for e in events if e.type == "StudyFinalized")
    contrib = fin.payload.get("contributing_reasons") or []
    assert "not done: expansion limit" in contrib
    report_id = fin.payload.get("report_artifact_id")
    if report_id and rt.artifacts is not None:
        md = rt.artifacts.read(report_id).decode("utf-8")
        assert "not done: expansion limit" in md
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
    await _boot_plan(
        rt,
        tasks=[
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
    await _boot_plan(
        rt,
        tasks=[
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
    await _boot_plan(
        rt,
        tasks=[
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
async def test_shared_descendant_not_skipped(tmp_path, clock, runtime_cls):
    """MJ5: task depended on by a new-question task is not SKIPPED."""
    sid = "st_share"
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
    await _boot_plan(
        rt,
        tasks=[
            TaskSpec(
                task_key="E0",
                title="shared",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E1",
                title="old-only",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                addresses=["Q1"],
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="old")],
    )
    # Don't start E0/E1 — both PENDING. New task depends on E0.
    await rt.expand_plan(
        after_task="PLAN",
        added_tasks=[
            TaskSpec(
                task_key="E2",
                title="new",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                addresses=["Q2"],
            )
        ],
        added_questions=[QuestionSpec(id="Q2", text="new")],
        superseded_questions=[QuestionSupersede(new_id="Q2", old_id="Q1")],
        origin="plan",
        delegated_by="human",
    )
    # E0 is needed by E2 → must remain PENDING (not superseded).
    assert rt.tasks[f"{sid}/E0"].state.value == "PENDING"
    # E1 only addresses Q1 and is not shared → SKIPPED.
    e1 = rt.tasks[f"{sid}/E1"]
    assert e1.state.value == "DONE"
    assert str(getattr(e1.reason, "value", e1.reason)) == TaskReason.SUPERSEDED.value
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
    await _boot_plan(
        rt,
        tasks=[
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
    await _boot_plan(
        rt,
        tasks=[
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
    for key in list(rt.tasks):
        t = rt.tasks[key]
        if t.state.value != "DONE":
            await rt.cancel_task(t.spec.task_key, reason="cancelled")
    if not rt._finalized:
        await rt.cancel_study()
    events = await rt.store.read_all()
    fin = next(e for e in events if e.type == "StudyFinalized")
    contrib = fin.payload.get("contributing_reasons") or []
    assert "not done: reserve" in contrib
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_replan_used_survives_restart(tmp_path, clock, runtime_cls):
    """B3: live == replay for replan_used + expansion counts; restart can't bypass."""
    sid = "st_restart"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=1,
        replan_reserve=0.5,
    )
    attach_executor(rt, tmp_path)
    await _boot_plan(
        rt,
        tasks=[
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
                title="a",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                addresses=["Q1"],
            )
        ],
        origin="emergent",
    )
    live_agent = rt._agent_expansions
    live_replan = rt._replan_used_s
    assert live_agent == 1
    assert live_replan > 0
    await close_runtime(rt)

    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls)
    attach_executor(rt2, tmp_path)
    assert rt2._agent_expansions == live_agent
    assert abs(rt2._replan_used_s - live_replan) < 1e-6
    # Restart cannot bypass agent quota.
    refused = await rt2.expand_plan(
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
    )
    assert refused is None
    events = await rt2.store.read_all()
    assert any(
        e.type == "ExpansionRefused" and e.payload.get("detail") == "agent_quota" for e in events
    )
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_infra_failed_does_not_expand(tmp_path, clock, runtime_cls):
    """MJ3: infra FAILED does not trigger expansion even with expansion_request."""
    from coscientist.core.domain import TaskOutcome, TaskState
    from coscientist.core.expand_trigger import maybe_expand_after_task_done

    sid = "st_infra"
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
    await _boot_plan(
        rt,
        tasks=[
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
    aid = await rt.start_attempt("E0")
    await rt.fail_attempt(aid, retryable=False, close_reason="crashed")
    tid = f"{sid}/E0"
    # Inject typed request on the (only) attempt as if agent asked to expand.
    rt.results[aid] = {
        "attempt_id": aid,
        "task_id": tid,
        "result_class": "infra",
        "expansion_request": ExpansionRequest(
            tasks=[
                TaskSpec(
                    task_key="E1",
                    title="x",
                    goal="g",
                    route="experiment",
                    depends_on=["E0"],
                    addresses=["Q1"],
                )
            ],
            reason="should_not_apply",
        ).model_dump(mode="json"),
    }
    task = rt.tasks[tid]
    assert task.state == TaskState.DONE
    assert task.outcome == TaskOutcome.FAILED
    await maybe_expand_after_task_done(rt, tid)
    assert not any(
        t.spec.task_key.startswith("PLAN-") and t.spec.task_key != "PLAN" for t in rt.tasks.values()
    )
    events = await rt.store.read_all()
    assert not any(e.type == "PlanExpanded" for e in events)
    await close_runtime(rt)


def test_core_evaluated_only_from_effect_not_meta():
    """MJ1: core_evaluated comes ONLY from effect; meta supplies producer/call_id."""

    class Meta:
        producer = "core"
        produced_by = type("P", (), {"call_id": "st/E0#1:1"})()
        artifact_id = "art1"
        # Deliberately stamp a conflicting flag — must be ignored.
        core_evaluated = False

    effect = {"core_evaluated": True, "producer": "agent", "ci_low": 0.0, "ci_high": 1.0}
    out = recompute_effect_provenance(effect, attempt_id="st/E0#1", artifact_meta=Meta())
    assert out is not None
    assert out["core_evaluated"] is True  # from effect, not meta's False
    assert out["producer"] == "core"  # from meta
    assert out["produced_by_call_id"] == "st/E0#1:1"
    assert out["countable"] is True

    # Without effect stamp, meta must not invent core_evaluated=True.
    effect2 = {"producer": "agent"}
    out2 = recompute_effect_provenance(effect2, attempt_id="st/E0#1", artifact_meta=Meta())
    assert out2 is not None
    assert out2.get("core_evaluated") in (False, None) or out2["core_evaluated"] is False
    assert out2["countable"] is False


def test_upcaster_studycreated_v6_to_v7_expansion_fields():
    v, p = upcast("StudyCreated", 6, {"study_id": "s", "max_replans": 4})
    assert v >= 7
    assert p["max_expansions"] == 4
    assert p["max_human_expansions"] == 10
    assert p["replan_reserve"] == 0.1
    assert p["max_depth"] == 8


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


def test_expansion_refused_idem_deterministic():
    from coscientist.core.events import make_idem

    a = make_idem("ExpansionRefused", "s", "E0", "invalid", "unknown dependency X")
    b = make_idem("ExpansionRefused", "s", "E0", "invalid", "unknown dependency X")
    assert a == b
