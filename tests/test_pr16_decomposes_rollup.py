"""M1 PR16: decomposes (acyclic) + rollup + subtask tree in report (DESIGN_M1 §10)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.decomposes import (
    DECOMPOSITION_COVERS_ITEM_ID,
    assert_decomposes_acyclic,
    leaf_ids,
    parent_map_from_questions,
)
from coscientist.core.dispositions import dispositions
from coscientist.core.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    OutputSpec,
    QuestionSpec,
    RequirementRef,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.minimal_report import render_markdown_report
from coscientist.core.plan_validate import validate_decomposes
from coscientist.core.reasons import Disposition, TaskReason
from coscientist.core.rubric import freeze_rubric
from coscientist.core.rubric_types import Rubric, RubricItem
from coscientist.core.upcasters import upcast
from tests.helpers import attach_executor, close_runtime, open_runtime

# --- helpers -----------------------------------------------------------------


def _decomp_rubric() -> Rubric:
    return freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id=DECOMPOSITION_COVERS_ITEM_ID,
                    text="декомпозиция покрывает исходный вопрос",
                    required=True,
                    check="judge",
                )
            ],
            aggregate="all_required",
        )
    )


def _exp(
    key: str,
    *,
    addresses: list[str] | None = None,
    rubric: Rubric | None = None,
    criteria: list[RequirementRef] | None = None,
) -> TaskSpec:
    outs = [
        OutputSpec(
            name="main",
            media_type="text/markdown",
            role="report",
            rubric=rubric,
        )
    ]
    return TaskSpec(
        task_key=key,
        title=key,
        goal="g",
        route="experiment",
        depends_on=["PLAN"],
        addresses=addresses or [],
        expected_outputs=outs,
        criteria=criteria
        if criteria is not None
        else ([RequirementRef(template_id="rubric_check")] if rubric else []),
    )


def _q(qid: str, text: str, *, parent: str | None = None, origin: str | None = None) -> QuestionSpec:
    return QuestionSpec(
        id=qid,
        text=text,
        parent_id=parent,
        origin=origin,  # type: ignore[arg-type]
        final_type="artifact",
    )


async def _boot(tmp_path, clock, sid: str):
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="t",
        request="decompose Q into subquestions",
        catalog_version="1",
        deadline=clock.now() + timedelta(hours=1),
        finalize_reserve_s=30,
        sid=sid,
        max_depth=8,
        max_tasks=64,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    return rt


def _task(
    key: str,
    *,
    outcome: TaskOutcome = TaskOutcome.SUCCESS,
    reason: TaskReason | None = None,
    addresses: list[str] | None = None,
) -> Task:
    return Task(
        task_id=f"tid:{key}",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key=key,
            title=key,
            goal="g",
            route="experiment",
            addresses=addresses or [],
        ),
        state=TaskState.DONE,
        outcome=outcome,
        reason=reason,
        deadline=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )


# --- pure validators ---------------------------------------------------------


def test_assert_decomposes_rejects_self_loop():
    with pytest.raises(ValueError, match="self-loop"):
        assert_decomposes_acyclic({"Q1": "Q1"})


def test_assert_decomposes_rejects_cycle():
    with pytest.raises(ValueError, match="cyclic"):
        assert_decomposes_acyclic({"A": "B", "B": "C", "C": "A"})


def test_leaf_ids_basic():
    parent_of = {"Q": None, "Q.a": "Q", "Q.b": "Q", "Q.a.1": "Q.a"}
    assert leaf_ids(parent_of) == ["Q.a.1", "Q.b"]


@pytest.mark.asyncio
async def test_validator_requires_named_subquestions(tmp_path, clock):
    """Empty subquestion text → PlanRefused + StudyFinalized."""
    rt = await _boot(tmp_path, clock, "pr16-named")
    rub = _decomp_rubric()
    q_root = _q("Q", "root question")
    q_empty = _q("Q.a", "   ", parent="Q", origin="explicit")
    pid = await rt.accept_plan(
        [_exp("E0", addresses=["Q.a"], rubric=rub)],
        questions=[q_root],
        subquestions=[q_empty],
        rollup="all",
    )
    assert pid == ""
    assert getattr(rt, "_finalized", False)
    events = await rt.store.read_all()
    assert any(e.type == "PlanRefused" for e in events)
    refused = next(e for e in events if e.type == "PlanRefused")
    assert "name/text required" in str(refused.payload.get("detail") or "")
    assert any(e.type == "StudyFinalized" for e in events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_validator_requires_decomposition_covers_rubric(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "pr16-rubric")
    # Artifact rubric without decomposition_covers item.
    other = freeze_rubric(
        Rubric(
            items=[RubricItem(id="exists", text="exists", required=True, check="judge")],
            aggregate="all_required",
        )
    )
    q_root = _q("Q", "root")
    q_a = _q("Q.a", "child A", parent="Q")
    pid = await rt.accept_plan(
        [_exp("E0", addresses=["Q.a"], rubric=other)],
        questions=[q_root],
        subquestions=[q_a],
    )
    assert pid == ""
    events = await rt.store.read_all()
    refused = next(e for e in events if e.type == "PlanRefused")
    assert DECOMPOSITION_COVERS_ITEM_ID in str(refused.payload.get("detail") or "")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_validator_rejects_uncovered_leaf(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "pr16-leaf")
    rub = _decomp_rubric()
    q_root = _q("Q", "root")
    q_a = _q("Q.a", "child A", parent="Q")
    q_b = _q("Q.b", "child B", parent="Q")
    # Only Q.a addressed — Q.b uncovered.
    pid = await rt.accept_plan(
        [_exp("E0", addresses=["Q.a"], rubric=rub)],
        questions=[q_root, q_a, q_b],
        rollup="all",
    )
    assert pid == ""
    events = await rt.store.read_all()
    detail = next(e for e in events if e.type == "PlanRefused").payload.get("detail")
    assert "Q.b" in str(detail) and "uncovered" in str(detail)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_writes_decomposes_edge(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "pr16-edge")
    rub = _decomp_rubric()
    q_root = _q("Q", "root")
    q_a = _q("Q.a", "child A", parent="Q", origin="explicit")
    pid = await rt.accept_plan(
        [_exp("E0", addresses=["Q.a"], rubric=rub)],
        questions=[q_root],
        subquestions=[q_a],
        rollup="any",
    )
    assert pid
    plan = rt.plans[pid]
    assert plan["rollup"] == "any"
    assert any(s.get("id") == "Q.a" for s in plan.get("subquestions") or [])
    edges = [e for e in await rt.store.read_all() if e.type == "GraphEdgeAdded"]
    decomp = [e for e in edges if (e.payload or {}).get("kind") == "decomposes"]
    assert len(decomp) == 1
    assert decomp[0].payload["attrs"]["origin"] == "explicit"
    # Writer version after main #24 (v9) → PR16 v10.
    accepted = next(e for e in await rt.store.read_all() if e.type == "PlanAccepted")
    assert accepted.schema_version == PLAN_ACCEPTED_SCHEMA_VERSION == 10
    await close_runtime(rt)


def test_rollup_rule():
    """all / any / majority / rubric parent disposition from children."""
    plans = {
        "p1": {
            "plan_id": "p1",
            "revision": 1,
            "rollup": "all",
            "questions": [
                {"id": "Q", "text": "root", "parent_id": None},
                {"id": "Q.a", "text": "a", "parent_id": "Q"},
                {"id": "Q.b", "text": "b", "parent_id": "Q"},
            ],
            "tasks": [
                {"task_key": "Ea", "addresses": ["Q.a"]},
                {"task_key": "Eb", "addresses": ["Q.b"]},
            ],
        }
    }
    tasks = {
        "tid:Ea": _task("Ea", addresses=["Q.a"]),
        "tid:Eb": _task("Eb", addresses=["Q.b"]),
    }
    # No counting claims → leaves not_attempted; all → parent not_attempted.
    d = dispositions(tasks=tasks, plans=plans)
    assert d["Q.a"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert d["Q"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert d["Q"].get("rollup") == "all"

    # Force leaf dispositions via synthetic override path: inject counting-like
    # rows by using apply_rollup directly on pre-built leaf map.
    from coscientist.core.rollup import apply_rollup

    leaves = {
        "Q": {"disposition": Disposition.NOT_ATTEMPTED.value, "reason": "no_task", "formulation": "root"},
        "Q.a": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "a"},
        "Q.b": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "b"},
    }
    rolled = apply_rollup(leaves, plans=plans)
    assert rolled["Q"]["disposition"] == Disposition.ANSWERED.value
    assert rolled["Q"]["reason"] == "rollup_all"

    plans_any = {**plans, "p1": {**plans["p1"], "rollup": "any"}}
    leaves_mixed = {
        "Q": {"disposition": Disposition.NOT_ATTEMPTED.value, "reason": "no_task", "formulation": "root"},
        "Q.a": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "a"},
        "Q.b": {"disposition": Disposition.NOT_ATTEMPTED.value, "reason": "no_task", "formulation": "b"},
    }
    rolled_any = apply_rollup(leaves_mixed, plans=plans_any)
    assert rolled_any["Q"]["disposition"] == Disposition.ANSWERED.value
    assert rolled_any["Q"]["reason"] == "rollup_any"

    plans_maj = {**plans, "p1": {**plans["p1"], "rollup": "majority"}}
    leaves_maj = {
        "Q": {"disposition": Disposition.NOT_ATTEMPTED.value, "reason": "no_task", "formulation": "root"},
        "Q.a": {"disposition": Disposition.REFUTED.value, "reason": None, "formulation": "a"},
        "Q.b": {"disposition": Disposition.REFUTED.value, "reason": None, "formulation": "b"},
        "Q.c": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "c"},
    }
    plans_maj["p1"] = {
        **plans_maj["p1"],
        "questions": plans_maj["p1"]["questions"]
        + [{"id": "Q.c", "text": "c", "parent_id": "Q"}],
    }
    rolled_maj = apply_rollup(leaves_maj, plans=plans_maj)
    assert rolled_maj["Q"]["disposition"] == Disposition.REFUTED.value
    assert rolled_maj["Q"]["reason"] == "rollup_majority"

    plans_rub = {**plans, "p1": {**plans["p1"], "rollup": "rubric"}}
    rolled_rub = apply_rollup(leaves, plans=plans_rub)
    # rubric: parent keeps own leaf disposition (not_attempted here).
    assert rolled_rub["Q"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert rolled_rub["Q"].get("rollup") == "rubric"


@pytest.mark.asyncio
async def test_report_subtask_tree_marks_emergent(tmp_path, clock):
    """Report «Дерево подзадач» shows emergent origin and struck superseded keys."""
    rt = await _boot(tmp_path, clock, "pr16-tree")
    rub = _decomp_rubric()
    q_root = _q("Q", "root")
    q_a = _q("Q.a", "planned child", parent="Q", origin="explicit")
    pid = await rt.accept_plan(
        [
            _exp("E0", addresses=["Q.a"], rubric=rub),
            _exp("E1", addresses=["Q.a"], rubric=rub),
        ],
        questions=[q_root],
        subquestions=[q_a],
        rollup="all",
    )
    assert pid
    # Simulate expansion adding emergent child + superseding E1.
    from coscientist.core.events import EventEnvelope, make_idem

    q_b = _q("Q.b", "emergent child", parent="Q", origin="emergent")
    # HITL expand path
    rid = "hitl_pr16_tree"
    tid = next(iter(rt.tasks))
    await rt._append(
        [
            EventEnvelope(
                study_id=rt.study.study_id,
                type="HitlOpened",
                entity_key=rid,
                idem_key=make_idem("HitlOpened", rid),
                payload={
                    "request_id": rid,
                    "kind": "plan_expansion",
                    "payload_ref": tid,
                    "deadline": (rt.clock.now() + timedelta(seconds=60)).isoformat(),
                    "study_id": rt.study.study_id,
                    "task_id": tid,
                    "expand_after_task": "E0",
                    "open": True,
                    "cost_rub": 0.0,
                },
                ts=rt.clock.now(),
            ),
            EventEnvelope(
                study_id=rt.study.study_id,
                type="HitlDecided",
                entity_key=rid,
                idem_key=make_idem("HitlDecided", rid),
                payload={
                    "request_id": rid,
                    "decision": "accept",
                    "by": "human",
                    "decided_by": "human",
                    "approved_by": "human",
                    "reason": "answered",
                    "spent_rub": 0.0,
                },
                ts=rt.clock.now(),
            ),
        ]
    )
    new_pid = await rt.expand_plan(
        after_task="E0",
        added_tasks=[_exp("E2", addresses=["Q.b"], rubric=rub)],
        added_questions=[q_b],
        superseded_tasks=["E1"],
        origin="emergent",
        reason="emergent subquestion",
        hitl_request_id=rid,
    )
    assert new_pid, "expand_plan refused (check ExpansionRefused in journal)"
    # Pure render check with folded plans / tasks (including superseded E1).
    md = render_markdown_report(
        study_id=rt.study.study_id,
        terminal_reason="completed",
        trigger="all_done",
        verdict="partial",
        contributing_reasons=[],
        question_dispositions={
            "Q": {"disposition": "not_attempted", "reason": "rollup_incomplete", "formulation": "root"},
            "Q.a": {
                "disposition": "not_attempted",
                "reason": "no_counting_claims",
                "formulation": "planned child",
            },
            "Q.b": {
                "disposition": "not_attempted",
                "reason": "no_task",
                "formulation": "emergent child",
            },
        },
        tasks=rt.tasks,
        plans=rt.plans,
    )
    assert "## Дерево подзадач" in md
    assert "origin=emergent" in md
    assert "`Q.b`" in md
    assert "~~`E1`~~" in md or "superseded" in md
    # PlanExpanded writer version after main #24 (v3) → PR16 v4.
    expanded = [e for e in await rt.store.read_all() if e.type == "PlanExpanded"]
    assert expanded
    assert expanded[-1].schema_version == PLAN_EXPANDED_SCHEMA_VERSION == 4
    await close_runtime(rt)


def test_upcaster_planaccepted_v9_to_v10_decomposes():
    v, p = upcast(
        "PlanAccepted",
        9,
        {"plan_id": "p", "tasks": [], "questions": [{"id": "Q", "text": "t"}], "loops": []},
    )
    assert v == 10
    assert p["rollup"] == "all"
    assert p["subquestions"] == []
    assert p["questions"][0].get("origin") is None
    assert p["questions"][0].get("parent_id") is None


def test_upcaster_planexpanded_v3_to_v4_decomposes():
    v, p = upcast(
        "PlanExpanded",
        3,
        {"plan_id": "p", "added_questions": [{"id": "Q.a", "text": "a", "parent_id": "Q"}]},
    )
    assert v == 4
    assert p["rollup"] == "all"
    assert p["added_questions"][0].get("origin") is None


def test_validate_decomposes_pure_cycle():
    with pytest.raises(ValueError, match="cyclic|self-loop"):
        validate_decomposes(
            [_exp("E0", addresses=["A"], rubric=_decomp_rubric())],
            [
                QuestionSpec(id="A", text="a", parent_id="B"),
                QuestionSpec(id="B", text="b", parent_id="A"),
            ],
            rollup="all",
        )
