"""M1 PR12: budget_share / deadline_share (DESIGN_M1 §5 / S4)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from coscientist.core.artifacts import make_tool_ref
from coscientist.core.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    Budget,
    ExpansionRequest,
    ForEachSpec,
    HitlPolicy,
    QuestionSpec,
    RequirementRef,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.expand_critic import critic_gate
from coscientist.core.graph import GraphNode
from coscientist.core.ids import hypothesis_node_id, task_id
from coscientist.core.reasons import TaskReason
from coscientist.core.recover import recover
from coscientist.core.shares import (
    BUDGET_EXHAUSTED_LINE,
    allocate_budget,
    apply_foreach_parent_shares,
    critical_path_deadline_share_sum,
    resolve_shares,
    split_parent_share,
    validate_budget_shares,
    validate_deadline_shares,
    validate_plan_shares,
)
from coscientist.core.upcasters import upcast
from tests.helpers import attach_executor, close_runtime, open_runtime, open_runtime_experimental


def _exp(key: str, *, depends_on: list[str] | None = None, **kw) -> TaskSpec:
    return TaskSpec(
        task_key=key,
        title=kw.get("title", key),
        goal=kw.get("goal", "g"),
        route="experiment",
        depends_on=depends_on or [],
        addresses=kw.get("addresses", ["Q1"]),
        criteria=[RequirementRef(template_id="value_match")] if kw.get("criteria", True) else [],
        budget_share=kw.get("budget_share"),
        deadline_share=kw.get("deadline_share"),
        foreach=kw.get("foreach"),
        required=kw.get("required", True),
    )


async def _boot(tmp_path, clock, sid: str, **create_kw):
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    kw = {
        "profile": "t",
        "request": "research question",
        "catalog_version": "1",
        "deadline": clock.now() + timedelta(hours=1),
        "finalize_reserve_s": 30,
        "sid": sid,
    }
    kw.update(create_kw)
    await rt.create_study(**kw)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    return rt


async def _ok(rt, arts, aid: str) -> None:
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


async def _upsert_hyps(rt, *, of_task: str, n: int) -> None:
    sid = rt.study.study_id
    now = rt.clock.now()
    for i in range(n):
        hid = f"H{i}"
        nid = hypothesis_node_id(sid, hid)
        node = GraphNode(
            node_id=nid,
            kind="hypothesis",
            study_id=sid,
            ref_id=hid,
            attrs={
                "statement": f"s{i}",
                "selected": True,
                "rank": i,
                "source_task": of_task,
            },
            created_at=now,
        )
        await rt._append(
            [
                EventEnvelope(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=nid,
                    idem_key=make_idem("GraphNodeUpserted", nid, "pr12"),
                    payload=node.model_dump(mode="json"),
                    ts=now,
                )
            ]
        )


# --- pure validators / split -------------------------------------------------


def test_shares_validator_sum():
    ok = [
        _exp("A", budget_share=0.4, deadline_share=0.5),
        _exp("B", budget_share=0.4, deadline_share=0.5, depends_on=["A"]),
    ]
    validate_plan_shares(ok)
    with pytest.raises(ValueError, match="budget_share"):
        validate_budget_shares(
            [_exp("A", budget_share=0.6), _exp("B", budget_share=0.5)]
        )
    parallel = [
        _exp("A", deadline_share=0.6),
        _exp("B", deadline_share=0.6),
        _exp("C", deadline_share=0.3, depends_on=["A", "B"]),
    ]
    assert critical_path_deadline_share_sum(parallel) == pytest.approx(0.9)
    validate_deadline_shares(parallel)
    chain = [
        _exp("A", deadline_share=0.5),
        _exp("B", deadline_share=0.4, depends_on=["A"]),
        _exp("C", deadline_share=0.2, depends_on=["B"]),
    ]
    with pytest.raises(ValueError, match="critical-path"):
        validate_deadline_shares(chain)


def test_share_split_equal_remainder():
    tasks = [_exp("A", budget_share=0.4), _exp("B"), _exp("C")]
    resolved = resolve_shares(tasks, "budget_share")
    assert resolved["A"] == pytest.approx(0.4)
    assert resolved["B"] == pytest.approx(0.3)
    assert resolved["C"] == pytest.approx(0.3)


def test_foreach_parent_share_split():
    parts = split_parent_share(0.6, 3)
    assert parts[0] == pytest.approx(0.2)
    assert parts[1] == pytest.approx(0.2)
    assert parts[2] == pytest.approx(0.2)
    assert split_parent_share(None, 2) == [None, None]
    parent = _exp("T", budget_share=0.5, deadline_share=0.4)
    stamped = apply_foreach_parent_shares(parent, [_exp("K0"), _exp("K1")])
    assert stamped[0].budget_share == pytest.approx(0.25)
    assert stamped[1].deadline_share == pytest.approx(0.2)


# --- runtime -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_tail_gets_remaining_time(tmp_path, clock):
    rt = await _boot(
        tmp_path,
        clock,
        "share-tail",
        deadline=clock.now() + timedelta(seconds=200),
        finalize_reserve_s=20,
        replan_reserve=0.0,
    )
    await rt.accept_plan(
        [
            _exp("E0", deadline_share=0.5, budget_share=0.5, criteria=False),
            _exp(
                "E1",
                depends_on=["E0"],
                deadline_share=0.5,
                budget_share=0.5,
                criteria=False,
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
    )
    attach_executor(rt, tmp_path)
    await rt.start_attempt("E0")
    t0 = rt.tasks[task_id(rt.study.study_id, "E0")]
    span0 = (t0.deadline - clock.now()).total_seconds()
    assert 80 <= span0 <= 100
    await rt.cancel_task("E0", reason="cancelled")
    clock.advance(timedelta(seconds=10))
    await rt.start_attempt("E1")
    t1 = rt.tasks[task_id(rt.study.study_id, "E1")]
    span1 = (t1.deadline - clock.now()).total_seconds()
    assert span1 > span0 - 15
    assert t1.deadline <= rt.study.effective_deadline()
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_reserve_untouchable(tmp_path, clock):
    rt = await _boot(
        tmp_path,
        clock,
        "share-reserve",
        deadline=clock.now() + timedelta(seconds=130),
        finalize_reserve_s=30,
        replan_reserve=0.5,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    await rt.accept_plan(
        [_exp("E0", deadline_share=1.0, budget_share=1.0, criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
    )
    aid = await rt.start_attempt("E0")
    t = rt.tasks[task_id(rt.study.study_id, "E0")]
    span = (t.deadline - clock.now()).total_seconds()
    assert 45 <= span <= 55
    assert Decimal(str(rt.attempts[aid]["allocated_budget_rub"])) == pytest.approx(
        Decimal("50")
    )
    alloc = allocate_budget(
        "E0",
        study=rt.study,
        spent_rub=Decimal("0"),
        plan_tasks=[_exp("E0", budget_share=1.0)],
        not_started={"E0"},
    )
    assert alloc == pytest.approx(Decimal("50"))
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_share_split_and_enforcement(tmp_path, clock):
    rt = await _boot(
        tmp_path,
        clock,
        "share-enforce",
        replan_reserve=0.0,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    await rt.accept_plan(
        [
            _exp("E0", budget_share=0.2, criteria=False),
            _exp("E1", budget_share=0.8, depends_on=["E0"], criteria=False),
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
    )
    aid = await rt.start_attempt("E0")
    tid = task_id(rt.study.study_id, "E0")
    cap = Decimal(str(rt.attempts[aid]["allocated_budget_rub"]))
    assert cap == pytest.approx(Decimal("20"))
    await rt.record_budget_debit(spent_rub=cap, request_id="d1", task_id=tid)
    t0 = rt.tasks[tid]
    assert t0.state == TaskState.DONE
    assert t0.reason == TaskReason.BUDGET
    assert t0.outcome == TaskOutcome.SKIPPED
    assert not rt._budget_exceeded
    assert not rt._finalized
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_exhaustion_closure(tmp_path, clock):
    rt = await _boot(
        tmp_path,
        clock,
        "share-exhaust",
        budget=Budget(max_cost_rub=Decimal("10")),
    )
    await rt.accept_plan(
        [_exp("E0", budget_share=1.0, criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
    )
    await rt.start_attempt("E0")
    await rt.record_budget_debit(spent_rub=Decimal("10"), request_id="x1")
    await rt.tick()
    assert rt._finalized
    events = await rt.store.read_all()
    fin = next(e for e in events if e.type == "StudyFinalized")
    assert fin.payload["trigger"] == "budget"
    assert BUDGET_EXHAUSTED_LINE in (fin.payload.get("contributing_reasons") or [])
    e0 = rt.tasks[task_id(rt.study.study_id, "E0")]
    assert e0.state == TaskState.DONE
    assert e0.reason == TaskReason.BUDGET
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_replay_equals_live(tmp_path, clock):
    rt = await _boot(
        tmp_path,
        clock,
        "share-replay",
        deadline=clock.now() + timedelta(seconds=200),
        finalize_reserve_s=20,
        replan_reserve=0.1,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    await rt.accept_plan(
        [
            _exp("E0", budget_share=0.3, deadline_share=0.4, criteria=False),
            _exp(
                "E1",
                budget_share=0.7,
                deadline_share=0.6,
                depends_on=["E0"],
                criteria=False,
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
    )
    aid = await rt.start_attempt("E0")
    live_cap = rt.attempts[aid]["allocated_budget_rub"]
    live_dl = rt.tasks[task_id(rt.study.study_id, "E0")].deadline
    sid = rt.study.study_id
    await close_runtime(rt)

    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await recover(rt2)
    assert rt2.attempts[aid]["allocated_budget_rub"] == live_cap
    assert rt2.attempts[aid]["share_renorm"] is True
    assert rt2.tasks[task_id(sid, "E0")].deadline == live_dl
    assert rt2._task_budget_alloc[task_id(sid, "E0")] == Decimal(str(live_cap))
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_foreach_children_share_parent_budget(tmp_path, clock):
    rt = await _boot(
        tmp_path,
        clock,
        "share-foreach",
        budget=Budget(max_cost_rub=Decimal("100")),
        replan_reserve=0.0,
        max_expansions=0,
    )
    _, arts = attach_executor(rt, tmp_path)
    await rt.accept_plan(
        [
            _exp("E0", criteria=False),
            _exp(
                "FE",
                depends_on=["E0"],
                budget_share=0.6,
                deadline_share=0.6,
                foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="all"),
                criteria=False,
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
    )
    await _upsert_hyps(rt, of_task="E0", n=3)
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    children = rt._foreach_children.get("FE") or []
    assert len(children) == 3
    for k in children:
        spec = rt.tasks[task_id(rt.study.study_id, k)].spec
        assert spec.budget_share == pytest.approx(0.2)
        assert spec.deadline_share == pytest.approx(0.2)
    aid_c = await rt.start_attempt(children[0])
    # Three children @ 0.2 each (E0 done); pool=100 → ~33.33 each.
    assert Decimal(str(rt.attempts[aid_c]["allocated_budget_rub"])) == pytest.approx(
        Decimal("33.3333"), rel=Decimal("0.01")
    )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_budget_hitl_delegation(tmp_path, clock):
    rt = await open_runtime_experimental(
        tmp_path, "share-hitl", clock=clock, owner="t1"
    )
    await rt.create_study(
        profile="t",
        request="research question",
        catalog_version="1",
        deadline=clock.now() + timedelta(hours=1),
        finalize_reserve_s=30,
        sid="share-hitl",
        hitl_policy=HitlPolicy(mode="interactive"),
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [_exp("E0", criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
    )
    rid = await rt.open_hitl("E0", kind="budget")
    await rt.answer_hitl(rid, decision="delegate")
    events = await rt.store.read_all()
    decided = [
        e
        for e in events
        if e.type == "HitlDecided" and e.payload.get("request_id") == rid
    ]
    assert decided
    assert decided[0].payload.get("delegated_by") == "human"
    assert decided[0].payload.get("approved_by") == "critic"
    node = rt.graph.get_node(f"{rt.study.study_id}:study")
    assert node is not None
    assert node.attrs.get("delegated_by") == "human"
    assert node.attrs.get("budget_hitl_delegated") is True
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_agent_cannot_raise_own_budget_share(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "share-agent")
    await rt.accept_plan(
        [_exp("E0", budget_share=0.2, addresses=["Q1"])],
        questions=[QuestionSpec(id="Q1", text="q")],
    )
    req = ExpansionRequest(
        tasks=[
            TaskSpec(
                task_key="TMP",
                title="x",
                goal="g",
                route="experiment",
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
                budget_share=0.9,
            )
        ],
        questions=[],
        superseded_tasks=[],
        superseded_questions=[],
        reason="want more money",
    )
    detail = critic_gate(rt, req, after_task="E0")
    assert detail is not None and "agent_share_raise" in detail
    req2 = ExpansionRequest(
        tasks=[
            TaskSpec(
                task_key="TMP",
                title="x",
                goal="g",
                route="experiment",
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
                budget_share=None,
            )
        ],
        questions=[],
        superseded_tasks=[],
        superseded_questions=[],
        reason="ok",
    )
    detail2 = critic_gate(rt, req2, after_task="E0")
    assert detail2 is None or "agent_share_raise" not in detail2
    await close_runtime(rt)


def test_upcast_task_created_shares():
    v, p = upcast(
        "TaskCreated",
        7,
        {
            "task_id": "t",
            "kind": "experiment",
            "spec": {
                "task_key": "E0",
                "title": "t",
                "goal": "g",
                "route": "experiment",
                "foreach": None,
                "foreach_item": None,
                "addresses": [],
                "inputs": [],
            },
            "state": "PENDING",
            "deadline": "2020-01-01T00:00:00+00:00",
        },
    )
    assert v >= 8
    assert p["spec"]["budget_share"] is None
    assert p["spec"]["deadline_share"] is None


def test_upcast_plan_accepted_shares():
    v, p = upcast(
        "PlanAccepted",
        5,
        {
            "plan_id": "p",
            "study_id": "s",
            "revision": 1,
            "hypothesis_id": None,
            "tasks": [
                {
                    "task_key": "E0",
                    "title": "t",
                    "goal": "g",
                    "route": "experiment",
                    "foreach": None,
                    "foreach_item": None,
                    "addresses": [],
                    "inputs": [],
                }
            ],
            "questions": [],
            "expand_after": [],
        },
    )
    assert v >= 6
    assert p["tasks"][0]["budget_share"] is None
