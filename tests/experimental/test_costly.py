"""HITL costly gate + report spend (experimental / pre-M2)."""
import pytest


@pytest.mark.parametrize("mode,answer,cost,limit,effect,by,msg", [
    ("fail", None, 0, 1000, "FAILED(human_timeout)", None, None),
    ("fail", "delegate", 100, None, "WAIT", None, "нет бюджета"),
    ("fail", "delegate", 1500, 1000, "WAIT", None, "превышает остаток"),
    ("fail", "delegate", 500, 1000, "RUN", "critic", None),
    ("delegate_capped", None, 500, 1000, "RUN", "critic", None),
    ("delegate_capped", "delegate", 1500, 1000, "SKIPPED(budget)", "critic", "превышает остаток"),
    ("delegate", None, 10**6, None, "RUN", "critic", None),
    ("delegate_capped", "approve", 10**6, None, "RUN", "human", None),
])
def test_costly_gate_modes(impl, mode, answer, cost, limit, effect, by, msg):
    budget = None if limit is None else impl.DelegatedBudget(limit)
    eff, ev = impl.costly_gate(mode, answer, cost, budget)
    assert eff == effect and ev["decided_by"] == by and ev["reason"]
    assert ev.get("human_message") == msg
    if eff == "RUN" and by == "critic":
        assert ev["delegated_by"] == ("human" if answer == "delegate" else "timeout")
        impl.settle(ev, budget, cost)
        assert ev["spent_rub"] == cost


def test_delegated_budget_counter(impl):
    b = impl.DelegatedBudget(1000)
    eff, ev = impl.costly_gate("fail", "delegate", 400, b)
    assert eff == "RUN" and ev["delegated_by"] == "human"
    impl.settle(ev, b, 300)                                          # actual debited, not estimate
    assert (b.spent_rub, b.remaining_rub, ev["spent_rub"]) == (300, 700, 300)
    eff, ev = impl.costly_gate("fail", "delegate", 800, b)
    assert eff == "WAIT" and ev["human_message"] == "превышает остаток"
    eff, ev = impl.costly_gate("fail", "delegate", 200, b)
    impl.settle(ev, b, 350)                                          # overspend: actual > estimate
    assert b.blocked and b.remaining_rub == 350
    eff, ev = impl.costly_gate("fail", "delegate", 10, b)
    assert eff == "WAIT" and ev["reason"] == "delegation_disabled" and "перерасход" in ev["human_message"]
    assert impl.costly_gate("fail", None, 10, b)[0] == "FAILED(human_timeout)"


@pytest.mark.asyncio
async def test_costly_gate_budget_spent_shown_in_report(tmp_path, clock):
    """fail-mode «реши сам», DelegatedBudget debit via events, report shows spent_rub."""
    from datetime import timedelta
    from decimal import Decimal

    from coscientist.core.execution.domain import Budget, GateConfig, HitlPolicy, TaskOutcome
    from coscientist.experimental.report import render
    from coscientist.experimental.runtime import StudyRuntime as ExpStudyRuntime
    from tests.helpers import attach_executor, close_runtime, open_runtime, record_success

    policy = HitlPolicy(
        mode="interactive",
        gates={
            "costly_or_nonreal": GateConfig(
                enabled=True, timeout_s=30, on_timeout="fail"
            )
        },
    )
    budget = Budget(
        delegated_costly=Decimal("1000"), deadline=clock.now() + timedelta(hours=1)
    )

    # No budget configured → WAIT + human_message, gate stays open
    rt0 = await open_runtime(
        tmp_path / "nb", "st_nb", clock=clock, runtime_cls=ExpStudyRuntime
    )
    await rt0.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_nb",
        hitl_policy=policy,
        require_hypotheses=False,
    )
    _, arts0 = attach_executor(rt0, tmp_path / "nb")
    await record_success(rt0, await rt0.start_attempt("SPEC"), arts0)
    rid0 = await rt0.open_hitl("PLAN", kind="costly_or_nonreal", cost_rub=100)
    await rt0.answer_hitl(rid0, "delegate")
    assert rt0.hitl[rid0].open is True
    note0 = [e for e in await rt0.store.read_all() if e.payload.get("request_id") == rid0]
    assert any(e.payload.get("reason") == "delegation_disabled" for e in note0)
    assert any(e.payload.get("human_message") == "нет бюджета" for e in note0)
    await close_runtime(rt0)

    rt = await open_runtime(
        tmp_path, "st_cost", clock=clock, runtime_cls=ExpStudyRuntime
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_cost",
        hitl_policy=policy,
        budget=budget,
        require_hypotheses=False,
    )
    _, arts = attach_executor(rt, tmp_path)
    await record_success(rt, await rt.start_attempt("SPEC"), arts)

    # «реши сам» with budget → critic, delegated_by=human; settle debits actual
    rid = await rt.open_hitl("PLAN", kind="costly_or_nonreal", cost_rub=400)
    await rt.answer_hitl(rid, "delegate")
    assert rt.hitl[rid].open is False
    decided = [
        e
        for e in await rt.store.read_all()
        if e.type == "HitlDecided" and e.payload.get("request_id") == rid
    ]
    assert decided and decided[-1].payload.get("delegated_by") == "human"
    settled = await rt.settle_delegated_spend(rid, actual_rub=350)
    assert settled["spent_rub"] == 350
    assert rt.delegated_budget is not None and rt.delegated_budget.spent_rub == 350
    report = render(await rt.store.read_all())
    assert report["hitl_spend"]["total_spent_rub"] == 350.0
    assert any(d["spent_rub"] == 350.0 for d in report["hitl_spend"]["decisions"])
    assert "350" in report["summary"]

    # actual > estimate → blocked; further «реши сам» → WAIT
    rid2 = await rt.open_hitl("PLAN", kind="costly_or_nonreal", cost_rub=200)
    await rt.answer_hitl(rid2, "delegate")
    await rt.settle_delegated_spend(rid2, actual_rub=400)
    assert rt.delegated_budget.blocked
    rid3 = await rt.open_hitl("HYP", kind="costly_or_nonreal", cost_rub=10)
    await rt.answer_hitl(rid3, "delegate")
    assert rt.hitl[rid3].open is True
    notice = [e for e in await rt.store.read_all() if e.payload.get("request_id") == rid3]
    assert any(e.payload.get("reason") == "delegation_disabled" for e in notice)
    assert any("перерасход" in (e.payload.get("human_message") or "") for e in notice)

    # timeout in fail mode → FAILED(human_timeout)
    rid4 = await rt.open_hitl("LIT", kind="costly_or_nonreal", timeout_s=1, cost_rub=10)
    clock.advance(timedelta(seconds=2))
    await rt.tick()
    assert rt.hitl[rid4].open is False
    assert rt.tasks["st_cost/LIT"].outcome == TaskOutcome.FAILED
    assert rt.tasks["st_cost/LIT"].reason == "human_timeout"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_delegated_budget_remaining_after_crash_recover(tmp_path, clock):
    """Regression: settled spend in BudgetDebited restores remaining after reopen.

    Also asserts post-recover gating: cost_rub > remaining (650) is blocked.
    """
    from datetime import timedelta
    from decimal import Decimal

    from coscientist.core.execution.domain import Budget, GateConfig, HitlPolicy
    from coscientist.experimental.runtime import StudyRuntime as ExpStudyRuntime
    from tests.helpers import attach_executor, close_runtime, open_runtime, record_success

    policy = HitlPolicy(
        mode="interactive",
        gates={
            "costly_or_nonreal": GateConfig(
                enabled=True, timeout_s=30, on_timeout="fail"
            )
        },
    )
    budget = Budget(
        delegated_costly=Decimal("1000"), deadline=clock.now() + timedelta(hours=1)
    )
    rt = await open_runtime(
        tmp_path, "st_bud_rec", clock=clock, runtime_cls=ExpStudyRuntime
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_bud_rec",
        hitl_policy=policy,
        budget=budget,
        require_hypotheses=False,
    )
    _, arts = attach_executor(rt, tmp_path)
    await record_success(rt, await rt.start_attempt("SPEC"), arts)
    rid = await rt.open_hitl("PLAN", kind="costly_or_nonreal", cost_rub=400)
    await rt.answer_hitl(rid, "delegate")
    await rt.settle_delegated_spend(rid, actual_rub=350)
    assert rt.delegated_budget is not None
    assert rt.delegated_budget.spent_rub == 350
    assert rt.delegated_budget.remaining_rub == 650
    await close_runtime(rt)

    rt2 = await open_runtime(
        tmp_path, "st_bud_rec", clock=clock, runtime_cls=ExpStudyRuntime
    )
    assert rt2.delegated_budget is not None
    assert rt2.delegated_budget.spent_rub == 350
    assert rt2.delegated_budget.remaining_rub == 650
    # cost_rub > 650 remaining → blocked (WAIT / exceeds remainder)
    rid_blocked = await rt2.open_hitl("PLAN", kind="costly_or_nonreal", cost_rub=651)
    await rt2.answer_hitl(rid_blocked, "delegate")
    assert rt2.hitl[rid_blocked].open is True
    notices = [
        e
        for e in await rt2.store.read_all()
        if e.payload.get("request_id") == rid_blocked
    ]
    assert any(e.payload.get("reason") == "delegation_disabled" for e in notices)
    assert any(
        e.payload.get("human_message") == "превышает остаток" for e in notices
    )
    await close_runtime(rt2)



@pytest.mark.asyncio
async def test_study_budget_exceeded_from_costly_spend(tmp_path, clock, runtime_cls):
    """record_budget_debit → fold → tick emits BudgetExceeded (core + exp)."""
    from datetime import timedelta
    from decimal import Decimal

    from coscientist.core.execution.domain import Budget
    from tests.helpers import close_runtime, open_runtime

    budget = Budget(
        max_cost_rub=Decimal("100"),
        delegated_costly=Decimal("100"),
        deadline=clock.now() + timedelta(hours=1),
    )
    sid = "st_study_bud"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, runtime_cls=runtime_cls, owner="t1"
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(minutes=30),
        sid=sid,
        budget=budget,
        finalize_reserve_s=5,
        require_hypotheses=False,
    )
    await rt.record_budget_debit(spent_rub=Decimal("100"), request_id="debit-1")
    assert rt._study_spent_rub == Decimal("100")
    assert not rt._budget_exceeded
    await rt.tick()
    types = [e.type for e in await rt.store.read_all()]
    assert "BudgetDebited" in types
    assert "BudgetExceeded" in types
    fins = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fins) == 1
    assert fins[0].payload["trigger"] == "budget"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_settle_delegated_spend_emits_study_budget(tmp_path, clock):
    """Exp settle → BudgetDebited → BudgetExceeded (wired emit, not getattr)."""
    from datetime import timedelta
    from decimal import Decimal

    from coscientist.core.execution.domain import Budget, GateConfig, HitlPolicy
    from coscientist.experimental.runtime import StudyRuntime as ExpStudyRuntime
    from tests.helpers import attach_executor, close_runtime, open_runtime, record_success

    policy = HitlPolicy(
        mode="interactive",
        gates={
            "costly_or_nonreal": GateConfig(
                enabled=True, timeout_s=30, on_timeout="fail"
            )
        },
    )
    budget = Budget(
        max_cost_rub=Decimal("100"),
        delegated_costly=Decimal("100"),
        deadline=clock.now() + timedelta(hours=1),
    )
    sid = "st_settle_bud"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, runtime_cls=ExpStudyRuntime, owner="t1"
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(minutes=30),
        sid=sid,
        hitl_policy=policy,
        budget=budget,
        finalize_reserve_s=5,
        require_hypotheses=False,
    )
    _, arts = attach_executor(rt, tmp_path)
    await record_success(rt, await rt.start_attempt("SPEC"), arts)
    rid = await rt.open_hitl("PLAN", kind="costly_or_nonreal", cost_rub=100)
    await rt.answer_hitl(rid, "delegate")
    await rt.settle_delegated_spend(rid, actual_rub=100)
    types = [e.type for e in await rt.store.read_all()]
    assert "BudgetDebited" in types
    assert "BudgetExceeded" in types
    assert rt._budget_exceeded
    await close_runtime(rt)
