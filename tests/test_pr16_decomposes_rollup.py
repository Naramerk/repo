"""M1 PR16: decomposes (acyclic) + rollup + subtask tree in report (DESIGN_M1 §10)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.artifacts import make_tool_ref
from coscientist.core.decomposes import (
    DECOMPOSITION_COVERS_ITEM_ID,
    assert_decomposes_acyclic,
    leaf_ids,
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
from coscientist.core.errors import LeaseLost
from coscientist.core.events import (
    PLAN_ACCEPTED_SCHEMA_VERSION,
    PLAN_EXPANDED_SCHEMA_VERSION,
)
from coscientist.core.minimal_report import render_markdown_report
from coscientist.core.plan_validate import validate_decomposes
from coscientist.core.reasons import Disposition, TaskReason, TerminalReason
from coscientist.core.recover import recover
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


def _assert_single_finalized(events: list) -> dict:
    """Exactly one StudyFinalized; report XOR minimal_report; non-empty conclusion_ids."""
    finals = [e for e in events if e.type == "StudyFinalized"]
    assert len(finals) == 1
    p = finals[0].payload or {}
    has_report = bool(p.get("report_artifact_id"))
    has_minimal = p.get("minimal_report") is not None
    assert has_report ^ has_minimal, f"report XOR minimal_report required: {has_report=} {has_minimal=}"
    cids = p.get("conclusion_ids") or []
    assert len(cids) >= 1, "conclusion_ids must be non-empty"
    return p


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
    refused = next(e for e in events if e.type == "PlanRefused")
    assert "name/text required" in str(refused.payload.get("detail") or "")
    _assert_single_finalized(events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_validator_requires_decomposition_covers_rubric(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "pr16-rubric")
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
    _assert_single_finalized(events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_validator_rejects_uncovered_leaf(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "pr16-leaf")
    rub = _decomp_rubric()
    q_root = _q("Q", "root")
    q_a = _q("Q.a", "child A", parent="Q")
    q_b = _q("Q.b", "child B", parent="Q")
    pid = await rt.accept_plan(
        [_exp("E0", addresses=["Q.a"], rubric=rub)],
        questions=[q_root, q_a, q_b],
        rollup="all",
    )
    assert pid == ""
    events = await rt.store.read_all()
    detail = next(e for e in events if e.type == "PlanRefused").payload.get("detail")
    assert "Q.b" in str(detail) and "uncovered" in str(detail)
    _assert_single_finalized(events)
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
    accepted = next(e for e in await rt.store.read_all() if e.type == "PlanAccepted")
    assert accepted.schema_version == PLAN_ACCEPTED_SCHEMA_VERSION == 10
    await close_runtime(rt)


def test_rollup_rule():
    """all / any / majority / rubric parent disposition from children."""
    from coscientist.core.decomposes import majority_threshold
    from coscientist.core.rollup import apply_rollup

    assert majority_threshold(2) == 2
    assert majority_threshold(4) == 3
    assert majority_threshold(3) == 2

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
    d = dispositions(tasks=tasks, plans=plans)
    assert d["Q.a"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert d["Q"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert d["Q"].get("rollup") == "all"

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
        "questions": plans_maj["p1"]["questions"] + [{"id": "Q.c", "text": "c", "parent_id": "Q"}],
    }
    rolled_maj = apply_rollup(leaves_maj, plans=plans_maj)
    assert rolled_maj["Q"]["disposition"] == Disposition.REFUTED.value
    assert rolled_maj["Q"]["reason"] == "rollup_majority"

    plans_rub = {**plans, "p1": {**plans["p1"], "rollup": "rubric"}}
    rolled_rub = apply_rollup(leaves, plans=plans_rub)
    assert rolled_rub["Q"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert rolled_rub["Q"].get("rollup") == "rubric"


def test_majority_ties_do_not_win():
    """Strict majority: 1:1 and 2:2 → no majority (never preference-tiebreak to answered)."""
    from coscientist.core.rollup import apply_rollup

    base_q = [
        {"id": "Q", "text": "root", "parent_id": None},
        {"id": "Q.a", "text": "a", "parent_id": "Q"},
        {"id": "Q.b", "text": "b", "parent_id": "Q"},
    ]
    plans_11 = {
        "p1": {
            "plan_id": "p1",
            "revision": 1,
            "rollup": "majority",
            "questions": base_q,
            "tasks": [],
        }
    }
    leaves_11 = {
        "Q": {"disposition": Disposition.NOT_ATTEMPTED.value, "reason": "no_task", "formulation": "root"},
        "Q.a": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "a"},
        "Q.b": {"disposition": Disposition.REFUTED.value, "reason": None, "formulation": "b"},
    }
    rolled_11 = apply_rollup(leaves_11, plans=plans_11)
    assert rolled_11["Q"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert rolled_11["Q"]["reason"] == "rollup_no_majority"

    plans_22 = {
        "p1": {
            "plan_id": "p1",
            "revision": 1,
            "rollup": "majority",
            "questions": base_q
            + [
                {"id": "Q.c", "text": "c", "parent_id": "Q"},
                {"id": "Q.d", "text": "d", "parent_id": "Q"},
            ],
            "tasks": [],
        }
    }
    leaves_22 = {
        "Q": {"disposition": Disposition.NOT_ATTEMPTED.value, "reason": "no_task", "formulation": "root"},
        "Q.a": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "a"},
        "Q.b": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "b"},
        "Q.c": {"disposition": Disposition.REFUTED.value, "reason": None, "formulation": "c"},
        "Q.d": {"disposition": Disposition.REFUTED.value, "reason": None, "formulation": "d"},
    }
    rolled_22 = apply_rollup(leaves_22, plans=plans_22)
    assert rolled_22["Q"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert rolled_22["Q"]["reason"] == "rollup_no_majority"


def _tree_plans(rollup: str, *, extra_q: list[dict] | None = None) -> dict:
    qs = [
        {"id": "Q", "text": "root", "parent_id": None},
        {"id": "Q.a", "text": "a", "parent_id": "Q"},
        {"id": "Q.b", "text": "b", "parent_id": "Q"},
    ]
    if extra_q:
        qs = qs + list(extra_q)
    return {
        "p1": {
            "plan_id": "p1",
            "revision": 1,
            "rollup": rollup,
            "questions": qs,
            "tasks": [],
        }
    }


def test_rollup_any_never_raises_above_parent_claims():
    """Adversarial PR 8b: parent claims=refuted + child answered → stay refuted."""
    from coscientist.core.rollup import apply_rollup

    leaves = {
        "Q": {
            "disposition": Disposition.REFUTED.value,
            "reason": None,
            "verdict": "refuted",
            "formulation": "root",
        },
        "Q.a": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "a"},
        "Q.b": {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "no_task",
            "formulation": "b",
        },
    }
    rolled = apply_rollup(leaves, plans=_tree_plans("any"))
    assert rolled["Q"]["disposition"] == Disposition.REFUTED.value
    assert rolled["Q"].get("rollup_clamped") is True
    assert rolled["Q"].get("rollup_would_have") == Disposition.ANSWERED.value


@pytest.mark.parametrize("rule", ["all", "any", "majority"])
def test_rollup_never_raises_rubric_not_met_parent(rule: str):
    """Blocker: rubric_not_met parent + answered child must stay rubric_not_met."""
    from coscientist.core.rollup import apply_rollup

    # majority needs ≥2 answering children to meet n//2+1 on 2 kids → both answered.
    leaves = {
        "Q": {
            "disposition": Disposition.RUBRIC_NOT_MET.value,
            "reason": "rubric_rejected",
            "verdict": Disposition.RUBRIC_NOT_MET.value,
            "formulation": "root",
        },
        "Q.a": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "a"},
        "Q.b": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "b"},
    }
    rolled = apply_rollup(leaves, plans=_tree_plans(rule))
    assert rolled["Q"]["disposition"] == Disposition.RUBRIC_NOT_MET.value
    assert rolled["Q"].get("rollup_clamped") is True
    assert rolled["Q"].get("rollup_would_have") == Disposition.ANSWERED.value


def test_rollup_incomplete_keeps_own_terminal_all_and_majority():
    """rollup_incomplete / no_majority must not wipe a terminal parent verdict."""
    from coscientist.core.rollup import apply_rollup

    own = {
        "disposition": Disposition.REFUTED.value,
        "reason": None,
        "verdict": "refuted",
        "formulation": "root",
    }
    # all + one child not_attempted → rollup_incomplete
    leaves_all = {
        "Q": dict(own),
        "Q.a": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "a"},
        "Q.b": {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "no_task",
            "formulation": "b",
        },
    }
    rolled_all = apply_rollup(leaves_all, plans=_tree_plans("all"))
    assert rolled_all["Q"]["disposition"] == Disposition.REFUTED.value
    assert rolled_all["Q"].get("rollup_clamped") is True
    assert rolled_all["Q"].get("rollup_would_have") == Disposition.NOT_ATTEMPTED.value

    # majority 1:1 → no majority
    leaves_maj = {
        "Q": dict(own),
        "Q.a": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "a"},
        "Q.b": {"disposition": Disposition.REFUTED.value, "reason": None, "formulation": "b"},
    }
    rolled_maj = apply_rollup(leaves_maj, plans=_tree_plans("majority"))
    assert rolled_maj["Q"]["disposition"] == Disposition.REFUTED.value
    assert rolled_maj["Q"].get("rollup_clamped") is True
    assert rolled_maj["Q"].get("rollup_would_have") == Disposition.NOT_ATTEMPTED.value


def test_rollup_can_lower_parent_claims():
    """Children rollup may lower a claims-based parent (answered → refuted via all)."""
    from coscientist.core.rollup import apply_rollup

    leaves = {
        "Q": {
            "disposition": Disposition.ANSWERED.value,
            "reason": None,
            "verdict": "supported",
            "formulation": "root",
        },
        "Q.a": {"disposition": Disposition.REFUTED.value, "reason": None, "formulation": "a"},
        "Q.b": {"disposition": Disposition.REFUTED.value, "reason": None, "formulation": "b"},
    }
    rolled = apply_rollup(leaves, plans=_tree_plans("all"))
    assert rolled["Q"]["disposition"] == Disposition.REFUTED.value
    assert rolled["Q"]["reason"] == "rollup_all"


def test_rollup_cycle_localized_not_study_wide():
    """Cycle must tag only the cyclic subtree — unrelated questions still roll up."""
    from coscientist.core.rollup import apply_rollup

    plans = {
        "p1": {
            "plan_id": "p1",
            "revision": 1,
            "rollup": "all",
            "questions": [
                {"id": "A", "text": "a", "parent_id": "B"},
                {"id": "B", "text": "b", "parent_id": "A"},
                {"id": "Q", "text": "root", "parent_id": None},
                {"id": "Q.a", "text": "child", "parent_id": "Q"},
            ],
            "tasks": [],
        }
    }
    # Non-terminal cycle nodes (not_attempted, no verdict) → rollup_cycle wipe.
    leaves = {
        "A": {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "no_task",
            "formulation": "a",
        },
        "B": {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "no_task",
            "formulation": "b",
        },
        "Q": {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "no_task",
            "formulation": "root",
        },
        "Q.a": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "a"},
    }
    rolled = apply_rollup(leaves, plans=plans)
    assert rolled["A"]["reason"] == "rollup_cycle"
    assert rolled["A"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert rolled["B"]["reason"] == "rollup_cycle"
    assert rolled["B"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert rolled["Q"]["disposition"] == Disposition.ANSWERED.value
    assert rolled["Q"]["reason"] == "rollup_all"
    assert rolled["Q.a"]["disposition"] == Disposition.ANSWERED.value
    # Must not raise into dispositions()' broad except (study-wide wipe).
    apply_rollup(leaves, plans=plans)  # idempotent, no ValueError


def test_rollup_cycle_keeps_own_terminal_like_incomplete():
    """Cycle node with own terminal disposition must clamp, not wipe (like incomplete)."""
    from coscientist.core.rollup import apply_rollup

    plans = {
        "p1": {
            "plan_id": "p1",
            "revision": 1,
            "rollup": "all",
            "questions": [
                {"id": "A", "text": "a", "parent_id": "B"},
                {"id": "B", "text": "b", "parent_id": "A"},
            ],
            "tasks": [],
        }
    }
    leaves = {
        "A": {
            "disposition": Disposition.REFUTED.value,
            "reason": None,
            "verdict": "refuted",
            "formulation": "a",
        },
        "B": {
            "disposition": Disposition.INCONCLUSIVE.value,
            "reason": TaskReason.ITERATION_CAP.value,
            "reason_detail": [TaskReason.ITERATION_CAP.value],
            "formulation": "b",
        },
    }
    rolled = apply_rollup(leaves, plans=plans)
    assert rolled["A"]["disposition"] == Disposition.REFUTED.value
    assert rolled["A"].get("rollup_clamped") is True
    assert rolled["A"].get("rollup_would_have") == Disposition.NOT_ATTEMPTED.value
    assert "rollup_cycle" in (rolled["A"].get("reason_detail") or [])
    assert rolled["B"]["disposition"] == Disposition.INCONCLUSIVE.value
    assert rolled["B"].get("rollup_clamped") is True
    assert "rollup_cycle" in (rolled["B"].get("reason_detail") or [])


def test_rollup_iteration_cap_parent_clamped():
    """PR11 iteration_cap (inconclusive, no verdict) is terminal for clamp."""
    from coscientist.core.rollup import apply_rollup

    own = {
        "disposition": Disposition.INCONCLUSIVE.value,
        "reason": TaskReason.ITERATION_CAP.value,
        "reason_detail": [TaskReason.ITERATION_CAP.value],
        # dispositions.py ~383-388: no verdict key
        "formulation": "root",
    }
    leaves = {
        "Q": dict(own),
        "Q.a": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "a"},
        "Q.b": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "b"},
    }
    for rule in ("all", "any", "majority"):
        rolled = apply_rollup(leaves, plans=_tree_plans(rule))
        assert rolled["Q"]["disposition"] == Disposition.INCONCLUSIVE.value, rule
        assert rolled["Q"]["reason"] == TaskReason.ITERATION_CAP.value, rule
        assert rolled["Q"].get("rollup_clamped") is True, rule
        assert rolled["Q"].get("rollup_would_have") == Disposition.ANSWERED.value, rule


def test_question_origin_implicit_vs_explicit():
    from coscientist.core.decomposes import question_origin_from_plans

    origins = question_origin_from_plans(
        {
            "p1": {
                "plan_id": "p1",
                "revision": 1,
                "questions": [
                    {"id": "Q", "text": "root"},
                    # parent_id set but not listed in subquestions → implicit
                    {"id": "Q.inferred", "text": "inferred", "parent_id": "Q"},
                ],
                "subquestions": [
                    {"id": "Q.named", "text": "named", "parent_id": "Q"},
                ],
            }
        }
    )
    assert origins["Q.named"] == "explicit"
    assert origins["Q.inferred"] == "implicit"
    assert origins["Q"] == "explicit"


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
    from coscientist.core.events import EventEnvelope, make_idem

    q_b = _q("Q.b", "emergent child", parent="Q", origin="emergent")
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
    assert "~~`E1`~~" in md
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


def test_expand_graph_rejects_cycle_via_existing_edges():
    """expand_graph must see the full decomposes tree, not only the new self-pair."""
    from coscientist.core.expand_graph import _decomposes_parent_map, question_graph_events
    from coscientist.core.graph import (
        GraphEdge,
        GraphNode,
        GraphStore,
        default_execution_registry,
    )
    from coscientist.core.ids import question_node_id

    class _RT:
        def __init__(self) -> None:
            self.graph = GraphStore(default_execution_registry())

    rt = _RT()
    sid = "st_cycle"
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    # Existing B→A, C→B (child→parent).
    for qid, parent in (("A", None), ("B", "A"), ("C", "B")):
        nid = question_node_id(sid, qid)
        rt.graph.upsert_node(
            GraphNode(
                node_id=nid,
                kind="question",
                study_id=sid,
                ref_id=qid,
                attrs={"formulation": qid},
                created_at=now,
            )
        )
        if parent:
            pn = question_node_id(sid, parent)
            eid = f"decomposes:{nid}:{pn}"
            rt.graph.add_edge(
                GraphEdge(
                    edge_id=eid,
                    kind="decomposes",
                    study_id=sid,
                    src_id=nid,
                    dst_id=pn,
                    attrs={"origin": "explicit"},
                    created_at=now,
                )
            )
    # Batch-local cycle X⇄Y (old code only checked {qid: parent, parent: None}).
    with pytest.raises(ValueError, match="cyclic|self-loop"):
        question_graph_events(
            rt,  # type: ignore[arg-type]
            sid=sid,
            pid="p_new",
            added_questions=[
                QuestionSpec(id="X", text="x", parent_id="Y", origin="emergent"),
                QuestionSpec(id="Y", text="y", parent_id="X", origin="emergent"),
            ],
            superseded_questions=[],
            known_texts={},
            now=now,
        )
    # Closing the existing chain (A→C while C→B→A) via full parent map.
    parent_of = _decomposes_parent_map(rt, sid)  # type: ignore[arg-type]
    assert parent_of["B"] == "A"
    assert parent_of["C"] == "B"
    parent_of["A"] = "C"
    with pytest.raises(ValueError, match="cyclic"):
        assert_decomposes_acyclic(parent_of)


async def _complete_report_and_finalize(rt, tmp_path) -> None:
    aid = await rt.start_attempt("REPORT")
    if aid is None:
        await rt.ensure_lease(ttl_s=10_000)
        await rt.tick()
        return
    ex, arts = attach_executor(rt, tmp_path)
    art = arts.put(
        b"# report\n",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="text/markdown",
    )
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="report",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        ),
    )
    await rt.ensure_lease(ttl_s=10_000)
    await rt.tick()


@pytest.mark.asyncio
async def test_rollup_run_finalizes_with_single_study_finalized(tmp_path, clock):
    """Full accept → leaf success → StudyFinalized with checklist asserts."""
    rt = await _boot(tmp_path, clock, "pr16-rollup-run")
    rub = freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id=DECOMPOSITION_COVERS_ITEM_ID,
                    text="декомпозиция покрывает исходный вопрос",
                    required=True,
                    check="code",
                    code_kind="artifact_exists",
                )
            ],
            aggregate="all_required",
        )
    )
    q_root = _q("Q", "root")
    q_a = _q("Q.a", "child A", parent="Q", origin="explicit")
    pid = await rt.accept_plan(
        [_exp("E0", addresses=["Q.a"], rubric=rub)],
        questions=[q_root],
        subquestions=[q_a],
        rollup="all",
    )
    assert pid
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("E0")
    assert aid is not None
    art = arts.put(
        b"# leaf\n",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="text/markdown",
    )
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="leaf done",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        ),
    )
    await _complete_report_and_finalize(rt, tmp_path)
    events = await rt.store.read_all()
    payload = _assert_single_finalized(events)
    qdisp = payload.get("question_dispositions") or {}
    # Parent has no own claims; leaf rubric_met → rollup all → answered.
    assert qdisp.get("Q.a", {}).get("disposition") == Disposition.ANSWERED.value
    assert qdisp.get("Q", {}).get("disposition") == Disposition.ANSWERED.value
    assert qdisp.get("Q", {}).get("rollup") == "all"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_internal_error_finalizes_once(tmp_path, clock, monkeypatch):
    """Real raised exception on recover → StudyFinalized(internal_error)."""
    import coscientist.core.loop as loop_mod

    rt = await _boot(tmp_path, clock, "pr16-internal")
    rub = _decomp_rubric()
    await rt.accept_plan(
        [_exp("E0", addresses=["Q.a"], rubric=rub)],
        questions=[_q("Q", "root")],
        subquestions=[_q("Q.a", "child", parent="Q", origin="explicit")],
        rollup="any",
    )
    attach_executor(rt, tmp_path)
    await close_runtime(rt)

    rt2 = await open_runtime(tmp_path, "pr16-internal", clock=clock, owner="t2")
    attach_executor(rt2, tmp_path)

    async def _boom(_runtime):
        raise RuntimeError("pr16_injected_loop_resume_boom")

    monkeypatch.setattr(loop_mod, "resume_pending_loops", _boom)
    await recover(rt2)
    events = await rt2.store.read_all()
    payload = _assert_single_finalized(events)
    assert payload.get("terminal_reason") == TerminalReason.INTERNAL_ERROR.value
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_recover_then_finalize_single(tmp_path, clock):
    """Crash mid-attempt → reopen/recover → complete → one StudyFinalized."""
    rt = await _boot(tmp_path, clock, "pr16-recover")
    rub = freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id=DECOMPOSITION_COVERS_ITEM_ID,
                    text="декомпозиция покрывает исходный вопрос",
                    required=True,
                    check="code",
                    code_kind="artifact_exists",
                )
            ],
            aggregate="all_required",
        )
    )
    await rt.accept_plan(
        [_exp("E0", addresses=["Q.a"], rubric=rub)],
        questions=[_q("Q", "root")],
        subquestions=[_q("Q.a", "child", parent="Q", origin="explicit")],
        rollup="all",
    )
    attach_executor(rt, tmp_path)
    aid0 = await rt.start_attempt("E0")
    assert aid0 is not None
    # Crash with an open attempt (no ResultRecorded).
    await close_runtime(rt)

    rt2 = await open_runtime(tmp_path, "pr16-recover", clock=clock, owner="t2")
    attach_executor(rt2, tmp_path)
    out = await recover(rt2)
    assert out["closed"] >= 1
    assert rt2.attempts[aid0].get("closed") is True
    assert rt2.attempts[aid0].get("close_reason") == "crashed"
    ex, arts = attach_executor(rt2, tmp_path)
    aid = await rt2.start_attempt("E0")
    assert aid is not None
    assert aid != aid0
    art = arts.put(
        b"# leaf\n",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="text/markdown",
    )
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="leaf done",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        ),
    )
    await _complete_report_and_finalize(rt2, tmp_path)
    _assert_single_finalized(await rt2.store.read_all())
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_expand_plan_propagates_lease_lost(tmp_path, clock, monkeypatch):
    """LeaseLost from _expand_plan must propagate — no ExpansionRefused mask."""
    import coscientist.core.expand as expand_mod

    rt = await _boot(tmp_path, clock, "pr16-lease")
    rub = _decomp_rubric()
    await rt.accept_plan(
        [
            _exp("E0", addresses=["Q.a"], rubric=rub),
            _exp("E1", addresses=["Q.a"], rubric=rub),
        ],
        questions=[_q("Q", "root")],
        subquestions=[_q("Q.a", "child", parent="Q", origin="explicit")],
        rollup="all",
    )

    async def _boom(*_a, **_k):
        raise LeaseLost("mocked lease loss inside _expand_plan")

    monkeypatch.setattr(expand_mod, "_expand_plan", _boom)
    before = await rt.store.read_all()
    n_refused_before = sum(1 for e in before if e.type == "ExpansionRefused")
    with pytest.raises(LeaseLost, match="mocked lease loss"):
        await rt.expand_plan(
            after_task="E0",
            added_tasks=[_exp("E2", addresses=["Q.b"], rubric=rub)],
            added_questions=[_q("Q.b", "emergent", parent="Q", origin="emergent")],
            superseded_tasks=["E1"],
            origin="emergent",
            reason="should lose lease",
        )
    after = await rt.store.read_all()
    n_refused_after = sum(1 for e in after if e.type == "ExpansionRefused")
    assert n_refused_after == n_refused_before, "LeaseLost must not write ExpansionRefused"
    await close_runtime(rt)


def test_graph_edge_decomposes_in_affected_tables():
    from coscientist.core.events import EventEnvelope
    from coscientist.core.projections import _affected_tables, fold_events

    assert "graph_edges" in _affected_tables("GraphEdgeAdded")
    sid = "st"
    ev = EventEnvelope(
        study_id=sid,
        type="GraphEdgeAdded",
        entity_key="decomposes:a:b",
        idem_key="idem",
        payload={
            "edge_id": "decomposes:a:b",
            "kind": "decomposes",
            "study_id": sid,
            "src_id": "a",
            "dst_id": "b",
            "attrs": {"origin": "explicit"},
        },
        ts=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        schema_version=1,
    )
    proj = fold_events([ev])
    assert "graph_edges" in proj
    assert "decomposes:a:b" in proj["graph_edges"]


@pytest.mark.asyncio
async def test_foreach_after_task_done_reraise_lease_lost(tmp_path, clock, monkeypatch):
    """foreach.py:453 — LeaseLost from _materialize_one must not become refuse."""
    from coscientist.core import foreach as foreach_mod
    from coscientist.core.domain import ForEachSpec
    from coscientist.core.foreach import maybe_foreach_after_task_done
    from coscientist.core.ids import task_id

    sid = "pr16-fe-lease-after"
    rt = await _boot(tmp_path, clock, sid)
    e0 = _exp("E0", addresses=["Q.a"], rubric=_decomp_rubric())
    fe = TaskSpec(
        task_key="FE",
        title="FE",
        goal="g",
        route="experiment",
        depends_on=["E0"],
        addresses=["Q.a"],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        criteria=[],
    )
    await rt.accept_plan(
        [e0, fe],
        questions=[_q("Q", "root"), _q("Q.a", "child", parent="Q", origin="explicit")],
        rollup="all",
    )
    attach_executor(rt, tmp_path)

    async def _noop(_runtime, _tmpl):
        return None

    # Finish E0 without materializing (noop), then boom on the explicit call.
    monkeypatch.setattr(foreach_mod, "_materialize_one", _noop)
    aid = await rt.start_attempt("E0")
    assert aid
    await rt.cancel_task("E0", reason="cancelled")

    async def _boom(_runtime, _tmpl):
        raise LeaseLost("mocked lease loss in maybe_foreach_after_task_done")

    monkeypatch.setattr(foreach_mod, "_materialize_one", _boom)
    before = sum(1 for e in await rt.store.read_all() if e.type == "ExpansionRefused")
    with pytest.raises(LeaseLost, match="maybe_foreach_after_task_done"):
        await maybe_foreach_after_task_done(rt, task_id(sid, "E0"))
    after = sum(1 for e in await rt.store.read_all() if e.type == "ExpansionRefused")
    assert after == before
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_materialize_ready_foreach_reraise_lease_lost(tmp_path, clock, monkeypatch):
    """foreach.py:479 — LeaseLost from _materialize_one must not become refuse."""
    from coscientist.core import foreach as foreach_mod
    from coscientist.core.domain import ForEachSpec
    from coscientist.core.foreach import materialize_ready_foreach

    sid = "pr16-fe-lease-ready"
    rt = await _boot(tmp_path, clock, sid)
    e0 = _exp("E0", addresses=["Q.a"], rubric=_decomp_rubric())
    fe = TaskSpec(
        task_key="FE",
        title="FE",
        goal="g",
        route="experiment",
        depends_on=["E0"],
        addresses=["Q.a"],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        criteria=[],
    )
    await rt.accept_plan(
        [e0, fe],
        questions=[_q("Q", "root"), _q("Q.a", "child", parent="Q", origin="explicit")],
        rollup="all",
    )
    attach_executor(rt, tmp_path)

    async def _noop(_runtime, _tmpl):
        return None

    monkeypatch.setattr(foreach_mod, "_materialize_one", _noop)
    aid = await rt.start_attempt("E0")
    assert aid
    await rt.cancel_task("E0", reason="cancelled")

    async def _boom(_runtime, _tmpl):
        raise LeaseLost("mocked lease loss in materialize_ready_foreach")

    monkeypatch.setattr(foreach_mod, "_materialize_one", _boom)
    before = sum(1 for e in await rt.store.read_all() if e.type == "ExpansionRefused")
    with pytest.raises(LeaseLost, match="materialize_ready_foreach"):
        await materialize_ready_foreach(rt)
    after = sum(1 for e in await rt.store.read_all() if e.type == "ExpansionRefused")
    assert after == before
    await close_runtime(rt)



@pytest.mark.asyncio
async def test_expand_trigger_reraise_lease_lost(tmp_path, clock, monkeypatch):
    """expand_trigger.py:266 — LeaseLost must not become ExpansionRefused."""
    from coscientist.core.expand_trigger import maybe_expand_after_task_done
    from coscientist.core.ids import task_id

    sid = "pr16-exp-trig-lease"
    rt = await _boot(tmp_path, clock, sid)
    rub = _decomp_rubric()
    await rt.accept_plan(
        [_exp("E0", addresses=["Q.a"], rubric=rub)],
        questions=[_q("Q", "root"), _q("Q.a", "child", parent="Q", origin="explicit")],
        rollup="all",
    )
    attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("E0")
    assert aid
    await rt.cancel_task("E0", reason="cancelled")

    async def _boom(_runtime, _tid):
        raise LeaseLost("mocked lease loss in maybe_expand_after_task_done")

    monkeypatch.setattr("coscientist.core.expand_trigger._maybe_expand", _boom)
    before = sum(1 for e in await rt.store.read_all() if e.type == "ExpansionRefused")
    with pytest.raises(LeaseLost, match="maybe_expand_after_task_done"):
        await maybe_expand_after_task_done(rt, task_id(sid, "E0"))
    after = sum(1 for e in await rt.store.read_all() if e.type == "ExpansionRefused")
    assert after == before
    await close_runtime(rt)



@pytest.mark.asyncio
async def test_finalize_reraise_lease_lost(tmp_path, clock, monkeypatch):
    """finalize.py — LeaseLost from rich payload must not fall to last-resort."""
    import coscientist.core.finalize as fin_mod

    sid = "pr16-fin-lease"
    rt = await _boot(tmp_path, clock, sid)
    rub = _decomp_rubric()
    await rt.accept_plan(
        [_exp("E0", addresses=["Q.a"], rubric=rub)],
        questions=[_q("Q", "root"), _q("Q.a", "child", parent="Q", origin="explicit")],
        rollup="all",
    )
    for key in list(rt.tasks):
        t = rt.tasks[key]
        if t.state != TaskState.DONE:
            await rt.cancel_task(t.spec.task_key, reason="cancelled")

    async def _boom(*_a, **_k):
        raise LeaseLost("mocked lease loss in finalize rich payload")

    monkeypatch.setattr(fin_mod, "_build_rich_payload", _boom)
    before = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    with pytest.raises(LeaseLost, match="finalize rich payload"):
        await rt.finalize(trigger="all_done")
    after = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(after) == len(before), "LeaseLost must not write StudyFinalized"
    assert not getattr(rt, "_finalized", False)
    await close_runtime(rt)
