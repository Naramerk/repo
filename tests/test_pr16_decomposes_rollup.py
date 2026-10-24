"""M1 PR16: decomposes (acyclic) + rollup + subtask tree in report (DESIGN_M1 §10)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from coscientist.core.claims.dispositions import dispositions
from coscientist.core.claims.graph import GraphNode
from coscientist.core.claims.reasons import Disposition, NotAttemptedReason, TaskReason, TerminalReason
from coscientist.core.claims.rubric import freeze_rubric
from coscientist.core.claims.rubric_types import Rubric, RubricItem
from coscientist.core.events.errors import LeaseLost
from coscientist.core.events.events import (
    PLAN_ACCEPTED_SCHEMA_VERSION,
    PLAN_EXPANDED_SCHEMA_VERSION,
    EventEnvelope,
    make_idem,
)
from coscientist.core.events.ids import hypothesis_node_id, task_id
from coscientist.core.events.upcasters import upcast
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    ForEachSpec,
    LoopPredicate,
    LoopSpec,
    OutputSpec,
    QuestionSpec,
    RequirementRef,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.execution.recover import recover
from coscientist.core.finalize.minimal_report import render_markdown_report
from coscientist.core.plan.decomposes import (
    DECOMPOSITION_COVERS_ITEM_ID,
    assert_decomposes_acyclic,
    leaf_ids,
)
from coscientist.core.plan.loop import capped_scope_questions, loop_status
from coscientist.core.plan.plan_validate import validate_decomposes
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
        catalog_version=3,
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
    assert accepted.schema_version == PLAN_ACCEPTED_SCHEMA_VERSION == 12
    await close_runtime(rt)


def test_rollup_rule():
    """all / any / majority / rubric parent disposition from children."""
    from coscientist.core.claims.rollup import apply_rollup
    from coscientist.core.plan.decomposes import majority_threshold

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
    from coscientist.core.claims.rollup import apply_rollup

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
    from coscientist.core.claims.rollup import apply_rollup

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
    from coscientist.core.claims.rollup import apply_rollup

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
    from coscientist.core.claims.rollup import apply_rollup

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
    from coscientist.core.claims.rollup import apply_rollup

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
    from coscientist.core.claims.rollup import apply_rollup

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
    from coscientist.core.claims.rollup import apply_rollup

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
    from coscientist.core.claims.rollup import apply_rollup

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


def test_loop_cap_skips_decomposes_parent_so_rollup_wins():
    """Parent with *no own tasks* under loop_capped stays rollup-eligible → answered."""
    from coscientist.core.claims.dispositions import apply_loop_cap_dispositions, dispositions
    from coscientist.core.claims.rollup import apply_rollup

    plans = _tree_plans("all")
    capped = {
        "Q": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
        "Q.a": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
        "Q.b": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
    }
    # Cap-every-id-before-rollup (old bug for no-own-task roots): stamped + clamped.
    buggy = {
        "Q": {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "no_task",
            "formulation": "root",
        },
        "Q.a": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "a"},
        "Q.b": {"disposition": Disposition.ANSWERED.value, "reason": None, "formulation": "b"},
    }
    apply_loop_cap_dispositions(buggy, capped)
    assert buggy["Q"]["disposition"] == Disposition.INCONCLUSIVE.value
    buggy_rolled = apply_rollup(buggy, plans=plans)
    assert buggy_rolled["Q"]["disposition"] == Disposition.INCONCLUSIVE.value
    assert buggy_rolled["Q"]["reason"] == TaskReason.ITERATION_CAP.value

    def _rub(*, task_keys, **_kw):
        return "met" if task_keys else None

    import coscientist.core.claims.dispositions as disp_mod

    # No task addresses Q — parent has no own covering tasks.
    plans_ft = {
        "p1": {
            "plan_id": "p1",
            "revision": 1,
            "rollup": "all",
            "questions": [
                {"id": "Q", "text": "root"},
                {"id": "Q.a", "text": "a", "parent_id": "Q", "final_type": "artifact"},
                {"id": "Q.b", "text": "b", "parent_id": "Q", "final_type": "artifact"},
            ],
            "subquestions": [
                {"id": "Q.a", "text": "a", "parent_id": "Q"},
                {"id": "Q.b", "text": "b", "parent_id": "Q"},
            ],
            "tasks": [
                {"task_key": "E0", "addresses": ["Q.a"]},
                {"task_key": "E1", "addresses": ["Q.b"]},
            ],
        }
    }
    tasks = {
        "tid:E0": _task("E0", outcome=TaskOutcome.SUCCESS, addresses=["Q.a"]),
        "tid:E1": _task("E1", outcome=TaskOutcome.SUCCESS, addresses=["Q.b"]),
    }
    orig = disp_mod.task_rubric_status
    disp_mod.task_rubric_status = _rub  # type: ignore[assignment]
    try:
        out = dispositions(tasks=tasks, plans=plans_ft, loop_capped_questions=capped)
    finally:
        disp_mod.task_rubric_status = orig  # type: ignore[assignment]
    assert out["Q.a"]["disposition"] == Disposition.ANSWERED.value
    assert out["Q.b"]["disposition"] == Disposition.ANSWERED.value
    assert out["Q"]["disposition"] == Disposition.ANSWERED.value
    assert out["Q"]["reason"] == "rollup_all"


def test_loop_cap_keeps_parent_with_own_task_despite_answered_children():
    """Parent with own covering task under cap must not be raised by answered kids."""
    import coscientist.core.claims.dispositions as disp_mod
    from coscientist.core.claims.dispositions import dispositions

    capped = {
        "Q": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
        "Q.a": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
        "Q.b": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
    }
    plans_ft = {
        "p1": {
            "plan_id": "p1",
            "revision": 1,
            "rollup": "all",
            "questions": [
                {"id": "Q", "text": "root"},
                {"id": "Q.a", "text": "a", "parent_id": "Q", "final_type": "artifact"},
                {"id": "Q.b", "text": "b", "parent_id": "Q", "final_type": "artifact"},
            ],
            "subquestions": [
                {"id": "Q.a", "text": "a", "parent_id": "Q"},
                {"id": "Q.b", "text": "b", "parent_id": "Q"},
            ],
            "tasks": [
                {"task_key": "E_ROOT", "addresses": ["Q"]},
                {"task_key": "E0", "addresses": ["Q.a"]},
                {"task_key": "E1", "addresses": ["Q.b"]},
            ],
        }
    }
    # Parent's own loop task never produced an answer (not_attempted / no claims);
    # children succeed via artifact rubric.
    tasks = {
        "tid:E_ROOT": _task("E_ROOT", outcome=TaskOutcome.SUCCESS, addresses=["Q"]),
        "tid:E0": _task("E0", outcome=TaskOutcome.SUCCESS, addresses=["Q.a"]),
        "tid:E1": _task("E1", outcome=TaskOutcome.SUCCESS, addresses=["Q.b"]),
    }

    def _rub(*, task_keys, **_kw):
        # Only child artifact questions get rubric_met; parent stays not_attempted
        # (final_type verdict → no rubric path) then capped.
        return "met" if task_keys and "E_ROOT" not in task_keys else None

    orig = disp_mod.task_rubric_status
    disp_mod.task_rubric_status = _rub  # type: ignore[assignment]
    try:
        out = dispositions(tasks=tasks, plans=plans_ft, loop_capped_questions=capped)
    finally:
        disp_mod.task_rubric_status = orig  # type: ignore[assignment]
    assert out["Q.a"]["disposition"] == Disposition.ANSWERED.value
    assert out["Q.b"]["disposition"] == Disposition.ANSWERED.value
    # Cap on parent with own tasks → inconclusive; clamp blocks answered children.
    assert out["Q"]["disposition"] == Disposition.INCONCLUSIVE.value
    assert out["Q"]["reason"] == TaskReason.ITERATION_CAP.value
    assert out["Q"].get("rollup_clamped") is True


def test_loop_cap_preserves_iteration_cap_when_children_incomplete_or_rubric():
    """Parent with own task under cap keeps iteration_cap if kids incomplete / rubric."""
    import coscientist.core.claims.dispositions as disp_mod
    from coscientist.core.claims.dispositions import dispositions
    from coscientist.core.execution.domain import TaskKind, TaskSpec

    capped = {
        "Q": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
        "Q.a": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
    }

    def _pending(key: str, *, addresses: list[str]) -> Task:
        return Task(
            task_id=f"tid:{key}",
            kind=TaskKind.EXPERIMENT,
            spec=TaskSpec(
                task_key=key,
                title=key,
                goal="g",
                route="experiment",
                addresses=addresses,
            ),
            state=TaskState.PENDING,
            outcome=None,
            reason=None,
            deadline=datetime.now(timezone.utc),
        )

    def _rub(*, task_keys, **_kw):
        return None

    orig = disp_mod.task_rubric_status
    disp_mod.task_rubric_status = _rub  # type: ignore[assignment]
    try:
        # (1) incomplete children + rollup=all → parent must keep iteration_cap
        plans_inc = {
            "p1": {
                "plan_id": "p1",
                "revision": 1,
                "rollup": "all",
                "questions": [
                    {"id": "Q", "text": "root"},
                    {"id": "Q.a", "text": "a", "parent_id": "Q"},
                ],
                "subquestions": [{"id": "Q.a", "text": "a", "parent_id": "Q"}],
                "tasks": [
                    {"task_key": "E_ROOT", "addresses": ["Q"]},
                    {"task_key": "E0", "addresses": ["Q.a"]},
                ],
            }
        }
        tasks_inc = {
            "tid:E_ROOT": _pending("E_ROOT", addresses=["Q"]),
            "tid:E0": _pending("E0", addresses=["Q.a"]),
        }
        out_inc = dispositions(
            tasks=tasks_inc, plans=plans_inc, loop_capped_questions=capped
        )
        assert out_inc["Q"]["disposition"] == Disposition.INCONCLUSIVE.value
        assert out_inc["Q"]["reason"] == TaskReason.ITERATION_CAP.value

        # (2) rollup=rubric must not wipe iteration_cap from a capped parent
        plans_rub = {
            "p1": {
                "plan_id": "p1",
                "revision": 1,
                "rollup": "rubric",
                "questions": [
                    {"id": "Q", "text": "root"},
                    {"id": "Q.a", "text": "a", "parent_id": "Q", "final_type": "artifact"},
                ],
                "subquestions": [{"id": "Q.a", "text": "a", "parent_id": "Q"}],
                "tasks": [
                    {"task_key": "E_ROOT", "addresses": ["Q"]},
                    {"task_key": "E0", "addresses": ["Q.a"]},
                ],
            }
        }
        tasks_rub = {
            "tid:E_ROOT": _pending("E_ROOT", addresses=["Q"]),
            "tid:E0": _task(
                "E0", outcome=TaskOutcome.SUCCESS, addresses=["Q.a"]
            ),
        }
        out_rub = dispositions(
            tasks=tasks_rub, plans=plans_rub, loop_capped_questions=capped
        )
        assert out_rub["Q"]["disposition"] == Disposition.INCONCLUSIVE.value
        assert out_rub["Q"]["reason"] == TaskReason.ITERATION_CAP.value
        assert out_rub["Q"].get("rollup") == "rubric"
    finally:
        disp_mod.task_rubric_status = orig  # type: ignore[assignment]


def test_rollup_equal_rank_keeps_own_without_false_clamp():
    """rolled_rank == own_rank: keep own reason; no fake rollup_clamped."""
    from coscientist.core.claims.rollup import apply_rollup

    leaves = {
        "Q": {
            "disposition": Disposition.INCONCLUSIVE.value,
            "reason": TaskReason.ITERATION_CAP.value,
            "reason_detail": [TaskReason.ITERATION_CAP.value],
            "formulation": "root",
        },
        "Q.a": {
            "disposition": Disposition.INCONCLUSIVE.value,
            "reason": TaskReason.ITERATION_CAP.value,
            "formulation": "a",
        },
        "Q.b": {
            "disposition": Disposition.INCONCLUSIVE.value,
            "reason": TaskReason.ITERATION_CAP.value,
            "formulation": "b",
        },
    }
    rolled = apply_rollup(leaves, plans=_tree_plans("all"))
    assert rolled["Q"]["disposition"] == Disposition.INCONCLUSIVE.value
    assert rolled["Q"]["reason"] == TaskReason.ITERATION_CAP.value
    assert rolled["Q"].get("rollup_clamped") is not True
    assert rolled["Q"].get("reason") != "rollup_clamped_to_own"


def test_rollup_lower_preserves_iteration_cap_in_detail():
    """Children may lower disposition; iteration_cap must remain in reason_detail."""
    from coscientist.core.claims.rollup import apply_rollup

    leaves = {
        "Q": {
            "disposition": Disposition.INCONCLUSIVE.value,
            "reason": TaskReason.ITERATION_CAP.value,
            "reason_detail": [TaskReason.ITERATION_CAP.value],
            "formulation": "root",
        },
        "Q.a": {"disposition": Disposition.REFUTED.value, "reason": None, "formulation": "a"},
        "Q.b": {"disposition": Disposition.REFUTED.value, "reason": None, "formulation": "b"},
    }
    rolled = apply_rollup(leaves, plans=_tree_plans("all"))
    assert rolled["Q"]["disposition"] == Disposition.REFUTED.value
    assert rolled["Q"]["reason"] == "rollup_all"
    assert TaskReason.ITERATION_CAP.value in (rolled["Q"].get("reason_detail") or [])
    assert rolled["Q"].get("rollup_clamped") is not True


@pytest.mark.asyncio
async def test_expand_runtime_task_keeps_parent_under_loop_cap(tmp_path, clock):
    """Parent task from PlanExpanded must keep cap even if plans[*].tasks omit it.

    ``question_tasks`` only reads plans blobs; expand writes TaskCreated into
    ``runtime.tasks``. dispositions must union those addresses into cover /
    skip_cap so answered children cannot bypass the limit.
    """
    import coscientist.core.claims.dispositions as disp_mod
    from coscientist.core.claims.dispositions import dispositions
    from coscientist.core.plan.question_tasks import question_tasks

    sid = "pr16-expand-cap"
    rt = await _boot(tmp_path, clock, sid)
    rub = _decomp_rubric()
    await rt.accept_plan(
        [
            _exp("E0", addresses=["Q.a"], rubric=rub),
            _exp("E1", addresses=["Q.a"], rubric=rub),
        ],
        questions=[_q("Q", "root"), _q("Q.a", "child", parent="Q", origin="explicit")],
        subquestions=[_q("Q.a", "child", parent="Q", origin="explicit")],
        rollup="all",
    )
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("E0")
    assert aid
    # Live SUCCESS cover for Q.a — cancelled/superseded keys are excluded from
    # cover (E1 superseded below must not inflate child cover / false answers).
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
    e0 = next(t for t in rt.tasks.values() if t.spec.task_key == "E0")
    # Judge rubric may leave E0 open; cover only needs a non-cancelled/superseded key.
    assert e0.reason not in (TaskReason.CANCELLED, TaskReason.SUPERSEDED)

    # Agent expand (no HITL): adds a task addressing the parent into runtime.tasks.
    new_pid = await rt.expand_plan(
        after_task="E0",
        added_tasks=[
            TaskSpec(
                task_key="E_ROOT",
                title="E_ROOT",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                addresses=["Q"],
                expected_outputs=[
                    OutputSpec(
                        name="main",
                        media_type="text/markdown",
                        role="report",
                        rubric=rub,
                    )
                ],
                criteria=[RequirementRef(template_id="rubric_check")],
            )
        ],
        added_questions=[],
        superseded_tasks=["E1"],
        origin="emergent",
        reason="parent own work via expand",
    )
    assert new_pid, "expand_plan refused"
    assert any(
        "Q" in (t.spec.addresses or []) and t.spec.task_key == "E_ROOT"
        for t in rt.tasks.values()
    )
    e1 = next(t for t in rt.tasks.values() if t.spec.task_key == "E1")
    assert e1.reason == TaskReason.SUPERSEDED
    # Accepted-only plans: no E_ROOT addresses (simulates plans[*].tasks gap).
    plans_accepted = {
        k: v
        for k, v in rt.plans.items()
        if not (v.get("added_tasks") or v.get("foreach") or v.get("loop"))
    }
    assert not question_tasks(plans_accepted).get("Q"), (
        "precondition: accepted plans must not list parent-covering tasks"
    )

    capped = {
        "Q": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
        "Q.a": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
    }

    def _rub(*, task_keys, **_kw):
        # Child artifact-met via live E0; parent stays not_attempted then capped.
        # Superseded E1 must not be required for cover.
        return "met" if task_keys and "E_ROOT" not in task_keys else None

    # Force Q.a to artifact so children answer via rubric_met.
    plans_ft = {
        k: {
            **v,
            "rollup": "all",
            "questions": [
                (
                    {**q, "final_type": "artifact"}
                    if isinstance(q, dict) and q.get("id") == "Q.a"
                    else q
                )
                for q in (v.get("questions") or [])
            ],
        }
        for k, v in plans_accepted.items()
    }
    # Ensure Q.a final_type artifact is present even if questions list was empty shape.
    for _pid, blob in plans_ft.items():
        qs = list(blob.get("questions") or [])
        by_id = {str(q.get("id")): dict(q) for q in qs if isinstance(q, dict)}
        by_id.setdefault("Q", {"id": "Q", "text": "root"})
        by_id["Q.a"] = {
            **by_id.get("Q.a", {"id": "Q.a", "text": "child", "parent_id": "Q"}),
            "final_type": "artifact",
            "parent_id": "Q",
        }
        blob["questions"] = list(by_id.values())
        blob["subquestions"] = [
            {"id": "Q.a", "text": "child", "parent_id": "Q"},
        ]
        # Strip any parent-covering tasks from the plan blob (keep only Q.a).
        blob["tasks"] = [
            t
            for t in (blob.get("tasks") or [])
            if isinstance(t, dict) and "Q" not in (t.get("addresses") or [])
        ]

    orig = disp_mod.task_rubric_status
    disp_mod.task_rubric_status = _rub  # type: ignore[assignment]
    try:
        out = dispositions(
            tasks=rt.tasks,
            plans=plans_ft,
            loop_capped_questions=capped,
        )
    finally:
        disp_mod.task_rubric_status = orig  # type: ignore[assignment]

    assert out["Q.a"]["disposition"] == Disposition.ANSWERED.value
    assert out["Q"]["disposition"] == Disposition.INCONCLUSIVE.value
    assert out["Q"]["reason"] == TaskReason.ITERATION_CAP.value
    await close_runtime(rt)


def test_cover_excludes_cancelled_superseded_yields_no_task():
    """Fold TaskDone.reason superseded/cancelled drops cover → no_task (not tasks_skipped).

    Combined cover used to keep dead keys so cancelled-only covering became
    ``tasks_skipped``; pruning restores empty cover / ``no_task``. Runtime-only
    live covering can still change ``no_task`` → ``tasks_failed``.
    """
    from coscientist.core.claims.dispositions import dispositions

    plans = {
        "p1": {
            "plan_id": "p1",
            "revision": 1,
            "questions": [{"id": "Q", "text": "root"}],
            "tasks": [{"task_key": "E_DEAD", "addresses": ["Q"]}],
        }
    }
    dead = {
        "tid:E_DEAD": _task(
            "E_DEAD",
            outcome=TaskOutcome.SKIPPED,
            reason=TaskReason.CANCELLED,
            addresses=["Q"],
        )
    }
    out_dead = dispositions(tasks=dead, plans=plans)
    assert out_dead["Q"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert out_dead["Q"]["reason"] == NotAttemptedReason.NO_TASK.value

    supers = {
        "tid:E_DEAD": _task(
            "E_DEAD",
            outcome=TaskOutcome.SKIPPED,
            reason=TaskReason.SUPERSEDED,
            addresses=["Q"],
        )
    }
    out_sup = dispositions(tasks=supers, plans=plans)
    assert out_sup["Q"]["reason"] == NotAttemptedReason.NO_TASK.value

    # Runtime-only live cover (not in plans[*].tasks) changes no_task → tasks_failed.
    plans_empty = {
        "p1": {
            "plan_id": "p1",
            "revision": 1,
            "questions": [{"id": "Q", "text": "root"}],
            "tasks": [],
        }
    }
    live_fail = {
        "tid:E_RT": _task(
            "E_RT",
            outcome=TaskOutcome.FAILED,
            reason=TaskReason.FAILED,
            addresses=["Q"],
        )
    }
    out_rt = dispositions(tasks=live_fail, plans=plans_empty)
    assert out_rt["Q"]["reason"] == NotAttemptedReason.TASKS_FAILED.value


def test_superseded_only_parent_cover_skips_loop_cap():
    """Parent addressed only by superseded/cancelled tasks must not get false cap."""
    from coscientist.core.claims.dispositions import dispositions

    capped = {
        "Q": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
        "Q.a": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
        "Q.b": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
    }
    plans = {
        "p1": {
            "plan_id": "p1",
            "revision": 1,
            "rollup": "all",
            "questions": [
                {"id": "Q", "text": "root"},
                {"id": "Q.a", "text": "a", "parent_id": "Q"},
                {"id": "Q.b", "text": "b", "parent_id": "Q"},
            ],
            "subquestions": [
                {"id": "Q.a", "text": "a", "parent_id": "Q"},
                {"id": "Q.b", "text": "b", "parent_id": "Q"},
            ],
            "tasks": [
                {"task_key": "E_SUPER", "addresses": ["Q"]},
                {"task_key": "E_CANCEL", "addresses": ["Q"]},
                {"task_key": "E0", "addresses": ["Q.a"]},
                {"task_key": "E1", "addresses": ["Q.b"]},
            ],
        }
    }
    tasks = {
        "tid:E_SUPER": _task(
            "E_SUPER",
            outcome=TaskOutcome.SKIPPED,
            reason=TaskReason.SUPERSEDED,
            addresses=["Q"],
        ),
        "tid:E_CANCEL": _task(
            "E_CANCEL",
            outcome=TaskOutcome.SKIPPED,
            reason=TaskReason.CANCELLED,
            addresses=["Q"],
        ),
        "tid:E0": _task("E0", outcome=TaskOutcome.SUCCESS, addresses=["Q.a"]),
        "tid:E1": _task("E1", outcome=TaskOutcome.SUCCESS, addresses=["Q.b"]),
    }
    out = dispositions(tasks=tasks, plans=plans, loop_capped_questions=capped)
    # Dead parent cover → skip_cap (no false iteration_cap on Q).
    assert out["Q"]["reason"] != TaskReason.ITERATION_CAP.value
    assert out["Q"].get("rollup_clamped") is not True


@pytest.mark.asyncio
async def test_foreach_cover_excludes_superseded_template(tmp_path, clock):
    """Real foreach: SUPERSEDED template drops from cover; children keep it."""
    from coscientist.core.claims.dispositions import dispositions

    sid = "pr16-fe-cover"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="foreach cover",
        catalog_version=3,
        deadline=clock.now() + timedelta(hours=1),
        finalize_reserve_s=30,
        sid=sid,
        max_expansions=0,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")

    e0 = TaskSpec(
        task_key="E0",
        title="E0",
        goal="g",
        route="experiment",
        depends_on=["PLAN"],
        addresses=[],
        criteria=[],
        required=False,
    )
    fe = TaskSpec(
        task_key="FE",
        title="FE",
        goal="g",
        route="experiment",
        depends_on=["E0"],
        addresses=["Q1"],
        criteria=[RequirementRef(template_id="value_match")],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
    )
    await rt.accept_plan([e0, fe], questions=[QuestionSpec(id="Q1", text="why")])
    now = rt.clock.now()
    for i, hid in enumerate(("H0", "H1")):
        nid = hypothesis_node_id(sid, hid)
        await rt._append(
            [
                EventEnvelope(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=nid,
                    idem_key=make_idem("GraphNodeUpserted", nid, "pr16-fe"),
                    payload=GraphNode(
                        node_id=nid,
                        kind="hypothesis",
                        study_id=sid,
                        ref_id=hid,
                        attrs={
                            "statement": f"s{i}",
                            "prediction": f"p{i}",
                            "selected": True,
                            "rank": i,
                            "source_task": "E0",
                        },
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                )
            ]
        )

    aid = await rt.start_attempt("E0")
    content = b'{"count": 1}'
    art = arts.put(content, make_tool_ref(f"{aid}:out"), name="main", media_type="application/json")
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 1},
        ),
    )
    children = list(rt._foreach_children.get("FE") or [])
    assert len(children) == 2
    fe_t = rt.tasks[task_id(sid, "FE")]
    assert fe_t.reason == TaskReason.SUPERSEDED

    # Cancel children → only SUPERSEDED template would remain; cover must be empty.
    for k in children:
        await rt.cancel_task(k, reason="cancelled")
    out = dispositions(tasks=rt.tasks, plans=rt.plans)
    assert out["Q1"]["reason"] == NotAttemptedReason.NO_TASK.value, out["Q1"]

    # Live foreach child restores cover (combined cover changes no_task).
    # Re-open a fresh study path is heavy; instead assert SUCCESS child via synthetic
    # overlay on the same folded tasks map.
    live_child = children[0]
    overlay = dict(rt.tasks)
    ch = overlay[task_id(sid, live_child)]
    overlay[task_id(sid, live_child)] = ch.model_copy(
        update={"reason": None, "outcome": TaskOutcome.FAILED, "state": TaskState.DONE}
    )
    out_live = dispositions(tasks=overlay, plans=rt.plans)
    assert out_live["Q1"]["reason"] == NotAttemptedReason.TASKS_FAILED.value
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_iter_cover_and_reopen_dispositions_deterministic(tmp_path, clock):
    """Real loop iters → capped_scope_questions; reopen/replay dispositions match."""
    from coscientist.core.claims.dispositions import dispositions

    sid = "pr16-loop-cover"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="loop cover",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=400),
        finalize_reserve_s=5,
        sid=sid,
        max_expansions=0,
        max_loop_iter=8,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        aid = await rt.start_attempt(key)
        content = b'{"count": 1}'
        art = arts.put(
            content, make_tool_ref(f"{aid}:out"), name="main", media_type="application/json"
        )
        await rt.executor.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
                values={"count": 1},
            ),
        )

    nid = hypothesis_node_id(sid, "H1")
    rt.graph.upsert_node(
        GraphNode(
            node_id=nid,
            kind="hypothesis",
            study_id=sid,
            ref_id="H1",
            attrs={"text": "H1"},
        )
    )
    body = [
        TaskSpec(
            task_key="BODY",
            title="BODY",
            goal="loop body",
            route="experiment",
            depends_on=[],
            addresses=["Q1"],
            criteria=[RequirementRef(template_id="value_match")],
        )
    ]
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="POST",
                title="after",
                goal="g",
                route="experiment",
                depends_on=["loop:L1"],
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
                required=False,
            )
        ],
        questions=[QuestionSpec(id="Q1", text="which hyp")],
        loops=[
            LoopSpec(
                loop_id="L1",
                body=body,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
                max_iter=2,
                scope_hypotheses=["H1"],
                scope_questions=["Q1"],
                depends_on=[],
            )
        ],
    )

    def _iter_tasks(runtime, iter_n: int):
        return [
            t
            for t in runtime.tasks.values()
            if t.spec.loop_id == "L1" and t.spec.loop_iter == iter_n
        ]

    for it in (1, 2):
        tasks = _iter_tasks(rt, it)
        assert len(tasks) == 1, f"expected 1 body task in iter {it}"
        for t in tasks:
            aid = await rt.start_attempt(t.spec.task_key)
            content = b'{"count": 1}'
            art = arts.put(
                content, make_tool_ref(f"{aid}:out"), name="main", media_type="application/json"
            )
            await rt.executor.record(
                aid,
                AgentTaskOutput(
                    summary="ok",
                    outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
                    values={"count": 1},
                ),
            )
    assert loop_status(rt, "L1") == "capped"
    capped = capped_scope_questions(rt)
    assert "Q1" in capped
    assert capped["Q1"].get("reason") == "iteration_cap"
    assert int(capped["Q1"].get("iter") or 0) >= 1

    live = dispositions(
        tasks=rt.tasks,
        plans=rt.plans,
        loop_capped_questions=capped,
    )
    assert live["Q1"]["disposition"] == Disposition.INCONCLUSIVE.value
    assert live["Q1"]["reason"] == TaskReason.ITERATION_CAP.value

    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    attach_executor(rt2, tmp_path)
    await recover(rt2)
    assert loop_status(rt2, "L1") == "capped"
    capped2 = capped_scope_questions(rt2)
    assert capped2["Q1"]["iter"] == capped["Q1"]["iter"]
    again = dispositions(
        tasks=rt2.tasks,
        plans=rt2.plans,
        loop_capped_questions=capped2,
    )
    assert again["Q1"] == live["Q1"]
    await close_runtime(rt2)


def test_question_origin_implicit_vs_explicit():
    from coscientist.core.plan.decomposes import question_origin_from_plans

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
    from coscientist.core.events.events import EventEnvelope, make_idem

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
    assert expanded[-1].schema_version == PLAN_EXPANDED_SCHEMA_VERSION == 5
    await close_runtime(rt)


def test_upcaster_planaccepted_v9_to_v10_decomposes():
    v, p = upcast(
        "PlanAccepted",
        9,
        {"plan_id": "p", "tasks": [], "questions": [{"id": "Q", "text": "t"}], "loops": []},
    )
    assert v == 12
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
    assert v == 5
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
    from coscientist.core.claims.graph import (
        GraphEdge,
        GraphNode,
        GraphStore,
        default_execution_registry,
    )
    from coscientist.core.events.ids import question_node_id
    from coscientist.core.plan.expand_graph import _decomposes_parent_map, question_graph_events

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
    import coscientist.core.plan.loop as loop_mod

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
    import coscientist.core.plan.expand as expand_mod

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
    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.events.projections import _affected_tables, fold_events

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
    from coscientist.core.events.ids import task_id
    from coscientist.core.execution.domain import ForEachSpec
    from coscientist.core.plan import foreach as foreach_mod
    from coscientist.core.plan.foreach import maybe_foreach_after_task_done

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
    from coscientist.core.execution.domain import ForEachSpec
    from coscientist.core.plan import foreach as foreach_mod
    from coscientist.core.plan.foreach import materialize_ready_foreach

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
    from coscientist.core.events.ids import task_id
    from coscientist.core.plan.expand_trigger import maybe_expand_after_task_done

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

    monkeypatch.setattr("coscientist.core.plan.expand_trigger._maybe_expand", _boom)
    before = sum(1 for e in await rt.store.read_all() if e.type == "ExpansionRefused")
    with pytest.raises(LeaseLost, match="maybe_expand_after_task_done"):
        await maybe_expand_after_task_done(rt, task_id(sid, "E0"))
    after = sum(1 for e in await rt.store.read_all() if e.type == "ExpansionRefused")
    assert after == before
    await close_runtime(rt)



async def _finalize_lease_fixture(tmp_path, clock, sid: str):
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
    return rt


@pytest.mark.asyncio
async def test_finalize_reraise_lease_lost(tmp_path, clock, monkeypatch):
    """finalize.py — LeaseLost from rich payload must not fall to last-resort."""
    import coscientist.core.finalize.finalize as fin_mod

    rt = await _finalize_lease_fixture(tmp_path, clock, "pr16-fin-lease")

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


@pytest.mark.asyncio
async def test_finalize_primary_report_path_lease_lost(tmp_path, clock, monkeypatch):
    """finalize.py:675/:695 — LeaseLost from _primary_report_path must reraise (no StudyFinalized)."""
    import coscientist.core.finalize.finalize as fin_mod

    rt = await _finalize_lease_fixture(tmp_path, clock, "pr16-fin-primary-ll")

    async def _boom(*_a, **_k):
        raise LeaseLost("mocked lease loss in _primary_report_path")

    monkeypatch.setattr(fin_mod, "_primary_report_path", _boom)
    before = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    with pytest.raises(LeaseLost, match="_primary_report_path"):
        await rt.finalize(trigger="all_done")
    after = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(after) == len(before), "LeaseLost must not write StudyFinalized"
    assert not getattr(rt, "_finalized", False)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_render_markdown_report_lease_lost(tmp_path, clock, monkeypatch):
    """finalize.py:675/:695 — LeaseLost from render_markdown_report must reraise; no StudyFinalized."""
    import coscientist.core.finalize.finalize as fin_mod

    rt = await _finalize_lease_fixture(tmp_path, clock, "pr16-fin-render-ll")

    def _boom(*_a, **_k):
        raise LeaseLost("mocked lease loss in render_markdown_report")

    monkeypatch.setattr(fin_mod, "render_markdown_report", _boom)
    before = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    with pytest.raises(LeaseLost, match="render_markdown_report"):
        await rt.finalize(trigger="all_done")
    after = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(after) == len(before), "LeaseLost must not write StudyFinalized"
    assert not getattr(rt, "_finalized", False)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_minimal_report_lease_lost(tmp_path, clock, monkeypatch):
    """finalize.py:675/:695 — LeaseLost from build_minimal_report after primary failure."""
    import coscientist.core.finalize.finalize as fin_mod

    rt = await _finalize_lease_fixture(tmp_path, clock, "pr16-fin-minimal-ll")

    async def _fail_primary(*_a, **_k):
        raise RuntimeError("primary path boom → enter minimal fallback")

    def _boom_minimal(**_k):
        raise LeaseLost("mocked lease loss in build_minimal_report")

    monkeypatch.setattr(fin_mod, "_primary_report_path", _fail_primary)
    monkeypatch.setattr(fin_mod, "build_minimal_report", _boom_minimal)
    before = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    with pytest.raises(LeaseLost, match="build_minimal_report"):
        await rt.finalize(trigger="all_done")
    after = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(after) == len(before), "LeaseLost must not write StudyFinalized"
    assert not getattr(rt, "_finalized", False)
    await close_runtime(rt)
