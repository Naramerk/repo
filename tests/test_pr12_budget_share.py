"""M1 PR12: budget_share / deadline_share (DESIGN_M1 §5 / S4)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from coscientist.core.claims.graph import GraphNode
from coscientist.core.claims.reasons import TaskReason
from coscientist.core.events.errors import CoreBugError, SandboxInfraError
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.ids import hypothesis_node_id, task_id
from coscientist.core.events.upcasters import upcast, writer_schema_version
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import (
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
from coscientist.core.execution.recover import recover
from coscientist.core.plan.expand_critic import critic_gate
from coscientist.core.plan.shares import (
    BUDGET_EXHAUSTED_LINE,
    BUDGET_SHARES_REFUSED_LINE,
    TASK_BUDGET_EXHAUSTED_LINE,
    TASK_BUDGET_OVERRUN_LINE,
    allocate_budget,
    apply_foreach_parent_shares,
    critical_path_deadline_share_sum,
    resolve_shares,
    split_parent_share,
    validate_budget_shares,
    validate_deadline_shares,
    validate_plan_shares,
)
from tests.helpers import (
    NEED_OFF,
    NEED_UNRECORDED,
    attach_executor,
    close_runtime,
    open_runtime,
    open_runtime_experimental,
)


def _exp(key: str, *, depends_on: list[str] | None = None, **kw) -> TaskSpec:
    """Build a TaskSpec that satisfies PlanValidator invariants (PR13).

    Catalog defaults do **not** count as criteria. ``criteria=False`` ⇒ no
    addresses by default. Addressing tasks always get an explicit RequirementRef
    so ``accept_plan`` / ``validate_final_types`` accept them.
    """
    from coscientist.core.plan.plan_validate import _has_criterion

    want_criteria = kw.get("criteria", True)
    if want_criteria is False:
        criteria: list = []
        addresses = kw.get("addresses", [])
    elif want_criteria is True:
        criteria = [RequirementRef(template_id="value_match")]
        addresses = kw.get("addresses", ["Q1"])
    else:
        criteria = list(want_criteria)
        addresses = kw.get("addresses", ["Q1"] if criteria else [])
    spec = TaskSpec(
        task_key=key,
        title=kw.get("title", key),
        goal=kw.get("goal", "g"),
        route="experiment",
        depends_on=depends_on or [],
        addresses=addresses,
        criteria=criteria,
        budget_share=kw.get("budget_share"),
        deadline_share=kw.get("deadline_share"),
        foreach=kw.get("foreach"),
        required=kw.get("required", True),
    )
    # Fail closed in the helper so accept_plan tests never rely on catalog defaults.
    if spec.addresses and not _has_criterion(spec):
        raise ValueError("PlanValidator: addressing task needs ≥1 explicit criterion")
    return spec


async def _boot(tmp_path, clock, sid: str, **create_kw):
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    kw = {
        "profile": "t",
        "request": "research question",
        "catalog_version": 3,
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
                    ts=now, schema_version=writer_schema_version("GraphNodeUpserted"), 
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
    # None template ⇒ one default share (1.0) split among N.
    assert split_parent_share(None, 2) == pytest.approx([0.5, 0.5])
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
     hypotheses=NEED_OFF, literature=NEED_OFF)
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
    # Explicit shares ≤ (1−reserve)=0.5; sole task still takes the full pool.
    await rt.accept_plan(
        [_exp("E0", deadline_share=0.5, budget_share=0.5, criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
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
        plan_tasks=[_exp("E0", budget_share=0.5)],
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
     hypotheses=NEED_OFF, literature=NEED_OFF)
    aid = await rt.start_attempt("E0")
    tid = task_id(rt.study.study_id, "E0")
    cap = Decimal(str(rt.attempts[aid]["allocated_budget_rub"]))
    assert cap == pytest.approx(Decimal("20"))
    # Equality is still within share; exceed only when spent > cap.
    await rt.record_budget_debit(spent_rub=cap, request_id="d0", task_id=tid)
    assert rt.tasks[tid].state == TaskState.RUNNING
    await rt.record_budget_debit(spent_rub=Decimal("0.01"), request_id="d1", task_id=tid)
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
        replan_reserve=0.0,
    )
    await rt.accept_plan(
        [_exp("E0", budget_share=1.0, criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
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
        replan_reserve=0.0,
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
     hypotheses=NEED_OFF, literature=NEED_OFF)
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
     hypotheses=NEED_OFF, literature=NEED_OFF)
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


async def _boot_hitl_budget(tmp_path, clock, sid: str):
    rt = await open_runtime_experimental(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="t",
        request="research question",
        catalog_version=3,
        deadline=clock.now() + timedelta(hours=1),
        finalize_reserve_s=30,
        sid=sid,
        replan_reserve=0.0,
        hitl_policy=HitlPolicy(mode="interactive"),
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            _exp("E0", budget_share=0.5, criteria=False),
            _exp("E1", budget_share=0.4, depends_on=["E0"], criteria=False),
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    return rt


@pytest.mark.asyncio
async def test_budget_hitl_delegate_applies_shares(tmp_path, clock):
    """Budget «реши сам» routes to _apply_budget_hitl (not plan expand)."""
    rt = await _boot_hitl_budget(tmp_path, clock, "share-hitl-delegate")
    rid = await rt.open_hitl("E0", kind="budget")
    await rt.answer_hitl(
        rid,
        decision="delegate",
        expansion={"budget_shares": {"E0": 0.35, "E1": 0.55}},
    )
    events = await rt.store.read_all()
    decided = [
        e
        for e in events
        if e.type == "HitlDecided" and e.payload.get("request_id") == rid
    ]
    assert decided
    assert decided[0].payload.get("delegated_by") == "human"
    assert decided[0].payload.get("approved_by") == "critic"
    hitl_node = rt.graph.get_node(f"{rt.study.study_id}:hitl:{rid}")
    assert hitl_node is not None
    assert hitl_node.attrs.get("delegated_by") == "human"
    assert hitl_node.attrs.get("approved_by") == "critic"
    study_node = rt.graph.get_node(f"{rt.study.study_id}:study")
    assert study_node is None or study_node.attrs.get("delegated_by") is None
    adj = [
        e
        for e in events
        if e.type == "BudgetSharesAdjusted" and e.payload.get("request_id") == rid
    ]
    assert adj and adj[0].payload["approved_by"] == "critic"
    assert adj[0].payload["budget_shares"]["E0"] == pytest.approx(0.35)
    assert rt.tasks[task_id(rt.study.study_id, "E0")].spec.budget_share == pytest.approx(
        0.35
    )
    # Must not expand the plan on budget delegate.
    assert not any(e.type == "PlanExpanded" for e in events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_budget_hitl_accept_applies_shares(tmp_path, clock):
    rt = await _boot_hitl_budget(tmp_path, clock, "share-hitl-accept")
    rid = await rt.open_hitl("E0", kind="budget")
    await rt.answer_hitl(
        rid,
        decision="accept",
        expansion={"budget_shares": {"E0": 0.3, "E1": 0.7}},
    )
    adj = [e for e in await rt.store.read_all() if e.type == "BudgetSharesAdjusted"]
    assert adj and adj[-1].payload["budget_shares"]["E0"] == pytest.approx(0.3)
    assert adj[-1].payload.get("approved_by") == "human"
    assert rt.tasks[task_id(rt.study.study_id, "E0")].spec.budget_share == pytest.approx(
        0.3
    )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_budget_hitl_shares_refused(tmp_path, clock):
    """Critic reject on budget approve → BudgetSharesRefused + report line."""
    rt = await _boot_hitl_budget(tmp_path, clock, "share-hitl-refuse")
    rid = await rt.open_hitl("E0", kind="budget")
    await rt.answer_hitl(
        rid,
        decision="accept",
        expansion={"budget_shares": {"E0": 0.9, "E1": 0.9}},
    )
    refused = [e for e in await rt.store.read_all() if e.type == "BudgetSharesRefused"]
    assert refused and refused[-1].payload.get("reason") == "critic_rejected"
    assert refused[-1].payload.get("request_id") == rid
    # Shares unchanged on refuse.
    assert rt.tasks[task_id(rt.study.study_id, "E0")].spec.budget_share == pytest.approx(
        0.5
    )
    await rt.cancel_study()
    await rt.tick()
    fin = next(e for e in await rt.store.read_all() if e.type == "StudyFinalized")
    assert BUDGET_SHARES_REFUSED_LINE in (fin.payload.get("contributing_reasons") or [])
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_agent_cannot_raise_own_budget_share(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "share-agent", replan_reserve=0.0)
    await rt.accept_plan(
        [_exp("E0", budget_share=0.2, addresses=["Q1"])],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
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


@pytest.mark.asyncio
async def test_concurrent_starts_reserve_outstanding(tmp_path, clock):
    """B2: allocated-but-unspent must not let two starts eat the reserve."""
    rt = await _boot(
        tmp_path,
        clock,
        "share-concurrent",
        replan_reserve=0.2,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    await rt.accept_plan(
        [
            _exp("E0", budget_share=0.4, criteria=False),
            _exp("E1", budget_share=0.4, criteria=False),
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    a0 = await rt.start_attempt("E0")
    a1 = await rt.start_attempt("E1")
    c0 = Decimal(str(rt.attempts[a0]["allocated_budget_rub"]))
    c1 = Decimal(str(rt.attempts[a1]["allocated_budget_rub"]))
    # Pool after 20% reserve = 80; equal peers → 40+40, reserve untouched.
    assert c0 == pytest.approx(Decimal("40"))
    assert c1 == pytest.approx(Decimal("40"))
    assert c0 + c1 == pytest.approx(Decimal("80"))
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_no_study_budget_means_no_task_cap(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "share-nobudget", replan_reserve=0.0)
    await rt.accept_plan(
        [_exp("E0", budget_share=1.0, criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    aid = await rt.start_attempt("E0")
    assert rt.attempts[aid]["allocated_budget_rub"] is None
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_zero_pool_refuses_start(tmp_path, clock):
    rt = await _boot(
        tmp_path,
        clock,
        "share-zeropool",
        replan_reserve=0.0,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    await rt.accept_plan(
        [_exp("E0", budget_share=0.0, criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    aid = await rt.start_attempt("E0")
    assert aid is None  # honest refusal — no exception, no AttemptStarted
    t = rt.tasks[task_id(rt.study.study_id, "E0")]
    assert t.state == TaskState.DONE
    assert t.reason == TaskReason.BUDGET
    assert t.outcome == TaskOutcome.SKIPPED
    tid = task_id(rt.study.study_id, "E0")
    events = await rt.store.read_all()
    assert not any(
        e.type == "AttemptStarted" and e.payload.get("task_id") == tid for e in events
    )
    # Study still closes with a report (StudyFinalized invariant).
    await rt.cancel_study()
    await rt.tick()
    fin = next(e for e in await rt.store.read_all() if e.type == "StudyFinalized")
    assert TASK_BUDGET_EXHAUSTED_LINE in (fin.payload.get("contributing_reasons") or [])
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_retry_keeps_fixed_task_cap(tmp_path, clock):
    """M3: first allocation is fixed; retries reuse it (spend accumulates)."""
    from coscientist.core.plan.shares import p2_share_payload_fields

    rt = await _boot(
        tmp_path,
        clock,
        "share-retry",
        replan_reserve=0.0,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    await rt.accept_plan(
        [_exp("E0", budget_share=0.4, criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    a1 = await rt.start_attempt("E0")
    tid = task_id(rt.study.study_id, "E0")
    cap = Decimal(str(rt.attempts[a1]["allocated_budget_rub"]))
    assert cap > 0
    await rt.record_budget_debit(spent_rub=Decimal("5"), request_id="s1", task_id=tid)
    # Fresh allocate would change with spend/outstanding; payload must keep fixed cap.
    fields = p2_share_payload_fields(rt, rt.tasks[tid])
    assert Decimal(str(fields["allocated_budget_rub"])) == cap
    assert rt._task_spent_rub[tid] == Decimal("5")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_task_budget_report_line(tmp_path, clock):
    rt = await _boot(
        tmp_path,
        clock,
        "share-taskline",
        replan_reserve=0.0,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    await rt.accept_plan(
        [_exp("E0", budget_share=0.1, criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    aid = await rt.start_attempt("E0")
    tid = task_id(rt.study.study_id, "E0")
    cap = Decimal(str(rt.attempts[aid]["allocated_budget_rub"]))
    await rt.record_budget_debit(spent_rub=cap + Decimal("1"), request_id="x", task_id=tid)
    await rt.cancel_study()
    await rt.tick()
    events = await rt.store.read_all()
    fin = next(e for e in events if e.type == "StudyFinalized")
    reasons = fin.payload.get("contributing_reasons") or []
    assert TASK_BUDGET_EXHAUSTED_LINE in reasons
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_executor_debits_cost_rub(tmp_path, clock):
    rt = await _boot(
        tmp_path,
        clock,
        "share-exec",
        replan_reserve=0.0,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    _, arts = attach_executor(rt, tmp_path)
    await rt.accept_plan(
        [_exp("E0", budget_share=0.5, criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    aid = await rt.start_attempt("E0")
    tid = task_id(rt.study.study_id, "E0")
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
            cost_rub=Decimal("5"),
        ),
    )
    assert rt._task_spent_rub.get(tid) == Decimal("5")
    assert rt._study_spent_rub >= Decimal("5")
    await close_runtime(rt)


def test_upcast_task_created_shares():
    from coscientist.core.events.errors import CatalogError

    # v7 without an explicit freeze map: v14 refuses (no soft {} pin).
    with pytest.raises(CatalogError, match="sandbox_check_limits_required"):
        upcast(
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
    # v8 with a real freeze: share defaults arrive at v9.
    v8, p8 = upcast(
        "TaskCreated",
        8,
        {
            "task_id": "t",
            "kind": "experiment",
            "spec": {
                "task_key": "E0",
                "title": "t",
                "goal": "g",
                "route": "experiment",
                "sandbox_timeout_s": None,
                "sandbox_memory_mb": None,
                "sandbox_check_limits": {
                    "artifact_present": {
                        "timeout_s": 5.0,
                        "memory_mb": 64,
                        "max_output_bytes": 1024,
                        "pids_max": 16,
                        "rlimit_nproc": 16,
                        "reap_timeout_s": 1.0,
                        "output_drain_join_s": 1.0,
                        "allow_network": False,
                        "allow_cgroup_degraded": False,
                    }
                },
                "sandbox_limits_note": None,
                "foreach": None,
                "foreach_item": None,
                "addresses": [],
                "inputs": [],
            },
            "state": "PENDING",
            "deadline": "2020-01-01T00:00:00+00:00",
        },
    )
    assert v8 >= 9
    assert p8["spec"]["budget_share"] is None


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
    assert v >= 7
    assert p["tasks"][0]["budget_share"] is None
    assert p["hypotheses"] == NEED_UNRECORDED


async def _plan_one(rt, *, share: float = 0.5):
    await rt.accept_plan(
        [_exp("E0", budget_share=share, criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["scientific", "infra", "core_bug", "catalog_error", "timeout", "same_digest"],
)
async def test_debit_cost_rub_on_every_result_path(tmp_path, clock, monkeypatch, path):
    """B1: cost_rub debits on scientific/timeout/same_digest/core_bug/catalog/infra."""
    from coscientist.core.events.digest import canonical_json, sha256_hex
    from coscientist.core.events.errors import CatalogError
    from coscientist.core.execution.domain import CheckResult, ReqStatus

    rt = await _boot(
        tmp_path,
        clock,
        f"share-debit-{path}",
        replan_reserve=0.0,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    ex, arts = attach_executor(rt, tmp_path)
    await _plan_one(rt)
    aid = await rt.start_attempt("E0")
    assert aid is not None
    tid = task_id(rt.study.study_id, "E0")
    cost = Decimal("3.5")
    out = AgentTaskOutput(summary="x", outputs={}, cost_rub=cost)

    if path == "scientific":
        content = b'{"count": 1}'
        art = arts.put(
            content, make_tool_ref(f"{aid}:out"), name="main", media_type="application/json"
        )
        out = AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 1},
            cost_rub=cost,
        )
    elif path == "infra":
        monkeypatch.setattr(
            "coscientist.core.execution.executor.evaluate_task_checks",
            lambda *a, **k: (_ for _ in ()).throw(
                SandboxInfraError("sandbox_unavailable:host")
            ),
        )
    elif path == "core_bug":
        monkeypatch.setattr(
            "coscientist.core.execution.executor.evaluate_task_checks",
            lambda *a, **k: (_ for _ in ()).throw(CoreBugError("boom")),
        )
    elif path == "catalog_error":
        monkeypatch.setattr(
            "coscientist.core.execution.executor.evaluate_task_checks",
            lambda *a, **k: [],
        )
        monkeypatch.setattr(
            "coscientist.core.execution.executor.compute_outcome",
            lambda *a, **k: (_ for _ in ()).throw(CatalogError("bad catalog")),
        )
    elif path == "timeout":
        timeout_check = CheckResult(
            check_id="t1",
            template_id="value_match",
            status=ReqStatus.FAILED,
            is_code=True,
            required=True,
            role="validity",
            effect={"timeout_kind": "wall_timeout"},
        )
        monkeypatch.setattr(
            "coscientist.core.execution.executor.evaluate_task_checks",
            lambda *a, **k: [timeout_check],
        )
    elif path == "same_digest":
        timeout_check = CheckResult(
            check_id="t1",
            template_id="value_match",
            status=ReqStatus.FAILED,
            is_code=True,
            required=True,
            role="validity",
            effect={"timeout_kind": "wall_timeout"},
        )
        monkeypatch.setattr(
            "coscientist.core.execution.executor.evaluate_task_checks",
            lambda *a, **k: [timeout_check],
        )
        await ex.record(aid, AgentTaskOutput(summary="to", outputs={}, cost_rub=Decimal("1")))
        # Force retry start after backoff window.
        clock.advance(timedelta(seconds=2))
        await rt.tick()
        # Prefer explicit start if tick did not fire.
        open_aids = [
            a
            for a in rt.tasks[tid].attempts
            if not rt.attempts.get(a, {}).get("closed")
        ]
        if not open_aids:
            aid = await rt.start_attempt("E0")
            assert aid is not None
        else:
            aid = open_aids[0]
        digest = sha256_hex(canonical_json({"mounted": {}}))
        # Seed prior timeout row digest if missing.
        prior = rt.results.get(rt.tasks[tid].attempts[0])
        if isinstance(prior, dict) and not prior.get("agent_output_digest"):
            prior["agent_output_digest"] = digest
        out = AgentTaskOutput(summary="same", outputs={}, cost_rub=cost)

    batches: list[list[str]] = []
    orig = ex._append

    async def capture(events, **kw):
        batches.append([e.type for e in events])
        return await orig(events, **kw)

    monkeypatch.setattr(ex, "_append", capture)
    await ex.record(aid, out)
    assert rt._task_spent_rub.get(tid, Decimal("0")) >= cost
    # Atomic: BudgetDebited in same batch as ResultRecorded.
    assert any(
        "ResultRecorded" in b and "BudgetDebited" in b for b in batches
    ), batches
    if path == "infra":
        row = rt.results.get(aid)
        assert row and row.get("charged") is False
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_budget_debit_atomic_crash_between_recover(tmp_path, clock):
    """B2: Result without Debit stays unrecovered spend; atomic batch recovers both."""
    sid = "share-atomic"
    rt = await _boot(
        tmp_path,
        clock,
        sid,
        replan_reserve=0.0,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    attach_executor(rt, tmp_path)
    await _plan_one(rt)
    aid = await rt.start_attempt("E0")
    assert aid is not None
    tid = task_id(rt.study.study_id, "E0")
    # Simulate crash between ResultRecorded and a *separate* BudgetDebited:
    # journal a ResultRecorded alone (old non-atomic path). Spend stays 0.
    now = rt.clock.now()
    await rt.writer.append_many(
        [
            EventEnvelope(
                study_id=rt.study.study_id,
                type="ResultRecorded",
                entity_key=f"r:{aid}",
                idem_key=make_idem("ResultRecorded", f"r:{aid}", "orphan"),
                payload={
                    "result_id": f"r:{aid}",
                    "attempt_id": aid,
                    "task_id": tid,
                    "outcome": "FAILED",
                    "computed_outcome": "FAILED",
                    "reason": "failed",
                    "provenance": "real",
                    "check_results": [],
                    "unassessed": [],
                    "artifacts": [],
                    "agent_claim": None,
                    "inputs": [],
                    "result_class": "scientific",
                    "charged": True,
                    "payload_digest": "x",
                },
                ts=now,
                schema_version=3,
            )
        ]
    )
    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    await recover(rt2)
    assert Decimal(str(getattr(rt2, "_task_spent_rub", {}).get(tid, 0))) == 0
    await close_runtime(rt2)

    # Fresh study: atomic path journals both; reopen sees the debit.
    sid3 = "share-atomic-ok"
    root3 = tmp_path / "ok"
    rt3 = await _boot(
        root3,
        clock,
        sid3,
        replan_reserve=0.0,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    ex, arts = attach_executor(rt3, root3)
    await _plan_one(rt3)
    aid3 = await rt3.start_attempt("E0")
    assert aid3 is not None
    tid3 = task_id(rt3.study.study_id, "E0")
    content = b'{"count": 1}'
    art = arts.put(
        content, make_tool_ref(f"{aid3}:out"), name="main", media_type="application/json"
    )
    await ex.record(
        aid3,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 1},
            cost_rub=Decimal("7"),
        ),
    )
    await close_runtime(rt3)
    rt4 = await open_runtime(root3, sid3, clock=clock, owner="t3")
    await recover(rt4)
    assert rt4._task_spent_rub.get(tid3) == Decimal("7")
    await close_runtime(rt4)


@pytest.mark.asyncio
async def test_single_attempt_budget_overrun(tmp_path, clock):
    """Spend past remaining limit → overrun flag, keep SUCCESS detail, no retries."""
    rt = await _boot(
        tmp_path,
        clock,
        "share-overrun",
        replan_reserve=0.0,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    ex, arts = attach_executor(rt, tmp_path)
    # Tiny share → small remaining limit.
    await rt.accept_plan(
        [_exp("E0", budget_share=0.05, criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    aid = await rt.start_attempt("E0")
    assert aid is not None
    tid = task_id(rt.study.study_id, "E0")
    limit = Decimal(str(rt.attempts[aid]["budget_limit_rub"]))
    assert limit > 0
    assert Decimal(str(rt.attempts[aid]["allocated_budget_rub"])) == limit
    content = b'{"count": 1}'
    art = arts.put(
        content, make_tool_ref(f"{aid}:out"), name="main", media_type="application/json"
    )
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 1},
            cost_rub=limit + Decimal("10"),
        ),
    )
    row = rt.results[aid]
    assert row.get("budget_overrun") is True
    assert row.get("outcome") == TaskOutcome.SUCCESS.value
    # Cap forbids retries: task closed, no retry_at.
    assert tid not in rt._retry_at
    assert rt.tasks[tid].state == TaskState.DONE
    assert rt.tasks[tid].outcome == TaskOutcome.SUCCESS
    assert rt._task_spent_rub[tid] == limit + Decimal("10")
    done = next(
        e
        for e in await rt.store.read_all()
        if e.type == "TaskDone" and e.payload.get("task_id") == tid
    )
    assert done.payload.get("budget_overrun") is True
    assert done.payload.get("detail") != "budget_overrun"
    assert done.payload.get("outcome") == TaskOutcome.SUCCESS.value
    await rt.cancel_study()
    await rt.tick()
    fin = next(e for e in await rt.store.read_all() if e.type == "StudyFinalized")
    reasons = fin.payload.get("contributing_reasons") or []
    assert TASK_BUDGET_OVERRUN_LINE in reasons
    assert not TASK_BUDGET_OVERRUN_LINE.startswith("не выполнено:")
    await close_runtime(rt)


def test_would_exceed_budget_limit_helper():
    from coscientist.core.plan.shares import would_exceed_budget_limit

    assert not would_exceed_budget_limit(
        spent_so_far=1, next_cost=2, limit=Decimal("10")
    )
    assert would_exceed_budget_limit(spent_so_far=9, next_cost=2, limit=Decimal("10"))
    assert not would_exceed_budget_limit(spent_so_far=0, next_cost=1, limit=None)


@pytest.mark.asyncio
async def test_start_attempt_none_not_recordable(tmp_path, clock):
    """Callers must not pass start_attempt()'s None into Executor.record."""
    from tests.helpers import record_success

    rt = await _boot(
        tmp_path,
        clock,
        "share-none-aid",
        replan_reserve=0.0,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    _, arts = attach_executor(rt, tmp_path)
    await rt.accept_plan(
        [_exp("E0", budget_share=0.0, criteria=False)],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    aid = await rt.start_attempt("E0")
    assert aid is None
    with pytest.raises(AssertionError, match="returned None"):
        await record_success(rt, aid, arts)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_zero_share_retry_fire_does_not_crash(tmp_path, clock):
    """M1: _fire_due_retries must not raise when share pool is zero."""
    rt = await _boot(
        tmp_path,
        clock,
        "share-retry-zero",
        replan_reserve=0.0,
        budget=Budget(max_cost_rub=Decimal("100")),
    )
    await rt.accept_plan(
        [
            _exp("E0", budget_share=0.0, criteria=False),
            _exp("E1", budget_share=1.0, criteria=False),
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    # Inject a due retry for the zero-share task (would crash pre-fix).
    tid = task_id(rt.study.study_id, "E0")
    rt._retry_at[tid] = clock.now() - timedelta(seconds=1)
    await rt.tick()  # must not raise
    assert rt.tasks[tid].state == TaskState.DONE
    assert rt.tasks[tid].reason == TaskReason.BUDGET
    await rt.cancel_study()
    await rt.tick()
    assert any(e.type == "StudyFinalized" for e in await rt.store.read_all())
    await close_runtime(rt)
