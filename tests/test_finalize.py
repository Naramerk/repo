"""PR 2: study finalizer, dispositions, minimal_report, upcasts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from coscientist.core.claims.dispositions import dispositions
from coscientist.core.claims.reasons import NotAttemptedReason, TaskReason, TerminalReason
from coscientist.core.events.digest import canonical_json
from coscientist.core.events.errors import Conflict, Idempotent, StudyClosedError
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.metrics import late_write_rejected_total
from coscientist.core.events.projections import fold_events
from coscientist.core.events.upcasters import upcast
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import (
    QuestionSpec,
    RequirementRef,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.finalize.finalize import (
    contributing_reasons_ordered,
    pick_finalize_trigger,
    terminal_reason_for,
)
from coscientist.core.finalize.finalize_nodes import conclusion_ids_for
from coscientist.core.finalize.minimal_report import MAX_BYTES, build_minimal_report
from coscientist.core.finalize.study_view import study_view
from coscientist.core.plan.question_tasks import question_tasks, questions_from_spec_output
from tests.helpers import attach_executor, close_runtime, open_runtime, record_success

_TS = datetime.fromisoformat("2026-01-01T00:00:00+00:00")


async def _boot(
    tmp: Path,
    clock,
    sid: str,
    *,
    reserve: int = 5,
    seconds: int = 60,
    runtime_cls: type | None = None,
):
    rt = await open_runtime(
        tmp, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research", request="q", catalog_version=3,
        deadline=clock.now() + timedelta(seconds=seconds), sid=sid,
        finalize_reserve_s=reserve,
    )
    return rt


async def _skip_all(rt, reason=TaskReason.CANCELLED):
    for t in list(rt.tasks.values()):
        if t.state != TaskState.DONE:
            await rt.cancel_task(t.spec.task_key, reason=reason)


def _fin(events):
    """Exactly one StudyFinalized; report XOR minimal_report; non-empty conclusions."""
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1
    fin = fins[0]
    p = fin.payload if isinstance(fin.payload, dict) else {}
    has_report = bool(p.get("report_artifact_id"))
    has_minimal = bool(p.get("minimal_report"))
    assert has_report ^ has_minimal, (
        f"expected report XOR minimal_report, got report={has_report} "
        f"minimal={has_minimal}"
    )
    cids = p.get("conclusion_ids")
    assert isinstance(cids, list) and len(cids) > 0
    return fin


def _task(key, *, outcome=TaskOutcome.SUCCESS, reason=None, required=True,
          kind=TaskKind.EXPERIMENT, state=TaskState.DONE):
    return Task(
        task_id=f"st/{key}", kind=kind,
        spec=TaskSpec(task_key=key, title=key, goal="g", route="experiment", required=required),
        state=state, outcome=outcome if state == TaskState.DONE else None,
        reason=reason, deadline=_TS,
    )


def _spec(key, addresses=None, deps=None):
    return TaskSpec(
        task_key=key, title=key, goal="g", route="experiment",
        depends_on=deps or ["PLAN"], addresses=addresses or [],
    # Explicit criterion — PlanValidator does not credit catalog defaults.
    criteria=[RequirementRef(template_id="artifact_present")] if addresses else [],
    )


def test_same_tick_tiebreak():
    assert pick_finalize_trigger(cancelled=True, budget=True, deadline=True) == "cancel"
    assert pick_finalize_trigger(budget=True, deadline=True) == "budget"
    assert pick_finalize_trigger(refused=True, deadline=True) == "intake"
    assert pick_finalize_trigger(deadline=True, all_done=True) == "deadline"


def test_terminal_reason_trigger_cancel():
    assert terminal_reason_for("cancel", tasks={}, question_dispositions={}) == TerminalReason.CANCELLED


def test_terminal_reason_trigger_budget():
    assert terminal_reason_for("budget", tasks={}, question_dispositions={}) == TerminalReason.BUDGET


def test_terminal_reason_trigger_deadline():
    assert terminal_reason_for("deadline", tasks={}, question_dispositions={}) == TerminalReason.DEADLINE


def test_terminal_reason_trigger_intake_refused():
    assert terminal_reason_for("intake", tasks={}, question_dispositions={}) == TerminalReason.REFUSED


def test_terminal_reason_trigger_internal_error():
    """Public payload: trigger=internal_error → TerminalReason.INTERNAL_ERROR (PR13)."""
    assert terminal_reason_for("internal_error", tasks={}, question_dispositions={}) == TerminalReason.INTERNAL_ERROR


def test_terminal_reason_trigger_all_done_completed():
    term = terminal_reason_for(
        "all_done", tasks={"a": _task("A")}, question_dispositions={}
    )
    assert term == TerminalReason.COMPLETED


def test_terminal_reason_trigger_all_done_human_timeout():
    t = _task("A", outcome=TaskOutcome.FAILED, reason=TaskReason.HUMAN_TIMEOUT)
    assert terminal_reason_for("all_done", tasks={"a": t}, question_dispositions={}) == TerminalReason.HUMAN_TIMEOUT


def test_all_done_not_attempted_is_completed_not_infeasible():
    term = terminal_reason_for(
        "all_done", tasks={"a": _task("A")},
        question_dispositions={"Q1": {"disposition": "not_attempted", "reason": "no_task"}},
    )
    assert term == TerminalReason.COMPLETED != TerminalReason.INFEASIBLE


def test_contributing_reasons_ordered_by_seq():
    def td(key, reason, seq):
        return UnstampedEvent(
            study_id="s", type="TaskDone", entity_key=key, idem_key=key,
            payload={"reason": reason}, ts=_TS, seq=seq,
        )
    assert contributing_reasons_ordered([td("a", "budget", 2), td("b", "cancelled", 1), td("c", "budget", 3)]) == [
        "cancelled", "budget", "не выполнено: бюджет задачи исчерпан",
    ]


def test_question_tasks_union_across_revisions():
    plans = {
        "p1": {"tasks": [{"task_key": "E1", "addresses": ["Q1"]}]},
        "p2": {"tasks": [{"task_key": "E2", "addresses": ["Q1", "Q2"]}]},
    }
    assert question_tasks(plans) == {"Q1": {"E1", "E2"}, "Q2": {"E2"}}


def test_task_without_address_belongs_to_study():
    cover = question_tasks({"p": {"tasks": [
        {"task_key": "E0", "addresses": []}, {"task_key": "E1", "addresses": ["Q1"]},
    ]}})
    assert cover == {"Q1": {"E1"}}


def test_not_attempted_reason_from_task_statuses():
    plans = {"p": {
        "questions": [{"id": f"Q{i}", "text": "t"} for i in (1, 2, 3, 4)],
        "tasks": [
            {"task_key": "A", "addresses": ["Q1"]},
            {"task_key": "B", "addresses": ["Q2"]},
            {"task_key": "C", "addresses": ["Q3"]},
            {"task_key": "D", "addresses": ["Q4"]},
        ],
    }}
    tasks = {
        "st/A": _task("A", outcome=TaskOutcome.FAILED, reason=TaskReason.FAILED),
        # cancelled/superseded are excluded from cover → no_task (PR16 follow-up).
        "st/B": _task("B", outcome=TaskOutcome.SKIPPED, reason=TaskReason.CANCELLED),
        "st/C": _task("C"),
        # Non-cancelled skip still counts → tasks_skipped.
        "st/D": _task("D", outcome=TaskOutcome.SKIPPED, reason=TaskReason.BUDGET),
    }
    d = dispositions(tasks=tasks, plans=plans)
    assert d["Q1"]["reason"] == NotAttemptedReason.TASKS_FAILED.value
    assert d["Q2"]["reason"] == NotAttemptedReason.NO_TASK.value
    assert d["Q3"]["reason"] == NotAttemptedReason.NO_COUNTING_CLAIMS.value
    assert d["Q4"]["reason"] == NotAttemptedReason.TASKS_SKIPPED.value


def test_questions_from_spec_output_wired():
    out = questions_from_spec_output(
        {"questions": [{"id": "Q1", "text": "one"}, {"formulation": "two"}, "three"]}
    )
    assert out == [{"id": "Q1", "text": "one"}, {"id": "Q2", "text": "two"}, {"id": "Q3", "text": "three"}]



def test_upcast_taskcreated_addresses_default():
    """Production upcasters collapsed to v1."""
    from coscientist.core.events.upcasters import UPCASTERS, writer_schema_version

    assert writer_schema_version("TaskCreated") == 1
    assert UPCASTERS.get("TaskCreated", {}) == {}
    v, payload = upcast("TaskCreated", 1, {"x": 1})
    assert v == 1 and payload == {"x": 1}

def test_upcast_taskcreated_input_mode_old_default():
    """Production upcasters collapsed to v1."""
    from coscientist.core.events.upcasters import UPCASTERS, writer_schema_version

    assert writer_schema_version("TaskCreated") == 1
    assert UPCASTERS.get("TaskCreated", {}) == {}
    v, payload = upcast("TaskCreated", 1, {"x": 1})
    assert v == 1 and payload == {"x": 1}

def test_upcast_planaccepted_questions_default():
    """Production upcasters collapsed to v1."""
    from coscientist.core.events.upcasters import UPCASTERS, writer_schema_version

    assert writer_schema_version("TaskCreated") == 1
    assert UPCASTERS.get("TaskCreated", {}) == {}
    v, payload = upcast("TaskCreated", 1, {"x": 1})
    assert v == 1 and payload == {"x": 1}

def test_upcast_taskcreated_strips_plan_criterion_role():
    """Production upcasters collapsed to v1."""
    from coscientist.core.events.upcasters import UPCASTERS, writer_schema_version

    assert writer_schema_version("TaskCreated") == 1
    assert UPCASTERS.get("TaskCreated", {}) == {}
    v, payload = upcast("TaskCreated", 1, {"x": 1})
    assert v == 1 and payload == {"x": 1}

def test_upcast_study_finalized_v1_adds_counts_and_refusal():
    """Production upcasters collapsed to v1."""
    from coscientist.core.events.upcasters import UPCASTERS, writer_schema_version

    assert writer_schema_version("TaskCreated") == 1
    assert UPCASTERS.get("TaskCreated", {}) == {}
    v, payload = upcast("TaskCreated", 1, {"x": 1})
    assert v == 1 and payload == {"x": 1}

def test_minimal_report_le_10kb():
    tasks = {f"st/T{i}": _task(f"T{i}", outcome=TaskOutcome.SKIPPED, reason=TaskReason.CANCELLED) for i in range(500)}
    qdisp = {
        f"Q{i}": {
            "disposition": "not_attempted",
            "reason": "no_task",
            "formulation": "x" * 400,
        }
        for i in range(200)
    }
    report = build_minimal_report(
        study_id="st_big", terminal_reason="cancelled", trigger="cancel",
        contributing_reasons=["cancelled"], verdict="failed", tasks=tasks, attempts={},
        question_dispositions=qdisp, fallback_reason="CASUnavailable",
    )
    assert report["truncated"] is True
    assert len(canonical_json(report)) <= MAX_BYTES


def test_minimal_report_schema_valid():
    report = build_minimal_report(
        study_id="st", terminal_reason="completed", trigger="all_done", contributing_reasons=[],
        verdict="partial", tasks={"st/A": _task("A")}, attempts={},
        question_dispositions={"Q1": {"disposition": "not_attempted", "reason": "no_task", "formulation": "q"}},
        fallback_reason=None,
    )
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "coscientist/schema/minimal_report.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    jsonschema.validate(report, schema)


def test_report_not_required_verdict():
    req = _task("R")
    rep = _task("REPORT", outcome=TaskOutcome.FAILED, reason=TaskReason.FAILED, required=False, kind=TaskKind.REPORT)
    assert study_view([req, rep], finalized=True).verdict == "fulfilled"


@pytest.mark.asyncio
async def test_finalize_on_all_done(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_ad")
    await _skip_all(rt)
    await rt.tick()
    assert _fin(await rt.store.read_all()).payload["trigger"] == "all_done"
    assert rt.view().phase == "closed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_at_deadline_minus_reserve(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_ed", reserve=5, seconds=20)
    await rt.ensure_lease(ttl_s=300)
    clock.advance(timedelta(seconds=15))
    await rt.tick()
    ev = _fin(await rt.store.read_all())
    assert ev.payload["trigger"] == "deadline" and ev.payload["terminal_reason"] == "deadline"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_after_long_downtime(tmp_path, clock):
    sid = "st_dt"
    rt = await _boot(tmp_path, clock, sid, reserve=5, seconds=20)
    await close_runtime(rt)
    clock.advance(timedelta(hours=48))
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    await rt.tick()
    assert rt.view().phase == "closed"
    assert _fin(await rt.store.read_all()).payload["trigger"] == "deadline"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_on_cancel(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_cx")
    await rt.cancel_study(by="user")
    ev = _fin(await rt.store.read_all())
    assert ev.payload["trigger"] == "cancel" and ev.payload["terminal_reason"] == "cancelled"
    assert rt.view().phase == "closed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_budget_trigger(tmp_path, clock):
    """Budget via BudgetExceeded event from note_budget_exceeded()."""
    rt = await _boot(tmp_path, clock, "st_bg")
    await rt.note_budget_exceeded()
    await rt.tick()
    assert _fin(await rt.store.read_all()).payload["trigger"] == "budget"
    assert any(
        t.reason == TaskReason.BUDGET and t.outcome == TaskOutcome.SKIPPED
        for t in rt.tasks.values()
    )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_task_budget_does_not_close_study(tmp_path, clock, runtime_cls):
    """TaskDone(budget) from a costly route must not close the whole study."""
    from coscientist.core.claims.reasons import CloseReason

    sid = "st_bg2"
    rt = await _boot(tmp_path, clock, sid, runtime_cls=runtime_cls)
    # Sibling plan tasks (no depends_on between them) so H-c does not cascade.
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="A", title="a", goal="g", route="experiment", depends_on=["PLAN"]
            ),
            TaskSpec(
                task_key="B", title="b", goal="g", route="experiment", depends_on=["PLAN"]
            ),
        ],
        questions=[],
    )
    a = rt.tasks[f"{sid}/A"]
    events: list[UnstampedEvent] = []
    for aid in a.attempts:
        if not rt.attempts.get(aid, {}).get("closed"):
            events.append(
                UnstampedEvent(
                    study_id=sid,
                    type="AttemptClosed",
                    entity_key=aid,
                    idem_key=make_idem(
                        "AttemptClosed", aid, CloseReason.BUDGET.value
                    ),
                    payload={
                        "attempt_id": aid,
                        "closed": True,
                        "close_reason": CloseReason.BUDGET.value,
                        "task_id": a.task_id,
                    },
                    ts=clock.now(),
                    schema_version=1,
                )
            )
    events.append(
        UnstampedEvent(
            study_id=sid,
            type="TaskDone",
            entity_key=a.task_id,
            idem_key=make_idem("TaskDone", a.task_id),
            payload={
                "task_id": a.task_id,
                "outcome": TaskOutcome.SKIPPED.value,
                "reason": TaskReason.BUDGET.value,
            },
            ts=clock.now(),
            schema_version=1,
        )
    )
    await rt._append(events)
    await rt.tick()
    assert rt.tasks[f"{sid}/B"].state != TaskState.DONE
    assert rt.view().phase != "closed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_budget_exceeded_survives_crash(tmp_path, clock, runtime_cls):
    """BudgetExceeded persisted → reopen finalizes trigger=budget (core + exp)."""
    sid = "st_bg_crash"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
    )
    await rt.note_budget_exceeded()
    assert rt._budget_exceeded
    assert any(e.type == "BudgetExceeded" for e in await rt.store.read_all())
    assert rt.view().phase != "closed"
    await close_runtime(rt)
    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    assert rt2._budget_exceeded
    await rt2.tick()
    assert _fin(await rt2.store.read_all()).payload["trigger"] == "budget"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_budget_debit_recheck_after_crash_reopen(tmp_path, clock, runtime_cls):
    """BudgetDebited only → crash → reopen → tick re-checks spend → trigger=budget."""
    from decimal import Decimal

    from coscientist.core.execution.domain import Budget

    sid = "st_bg_debit_crash"
    budget = Budget(
        max_cost_rub=Decimal("50"),
        deadline=clock.now() + timedelta(seconds=60),
    )
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        budget=budget,
        finalize_reserve_s=5,
    )
    # Public debit without BudgetExceeded (crash before producer tick).
    await rt.record_budget_debit(spent_rub=Decimal("50"), request_id="debit-crash")
    assert rt._study_spent_rub == Decimal("50")
    assert not rt._budget_exceeded
    assert not any(e.type == "BudgetExceeded" for e in await rt.store.read_all())
    await close_runtime(rt)

    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    assert rt2._study_spent_rub == Decimal("50")
    assert not rt2._budget_exceeded
    await rt2.tick()
    assert rt2._budget_exceeded
    assert _fin(await rt2.store.read_all()).payload["trigger"] == "budget"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_create_study_rejects_delegated_above_max(tmp_path, clock, runtime_cls):
    from decimal import Decimal

    from coscientist.core.execution.domain import Budget

    sid = "st_bg_bad"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    with pytest.raises(ValueError, match="delegated_costly"):
        await rt.create_study(
            profile="research",
            request="q",
            catalog_version=3,
            deadline=clock.now() + timedelta(seconds=60),
            sid=sid,
            budget=Budget(
                max_cost_rub=Decimal("100"),
                delegated_costly=Decimal("101"),
            ),
            finalize_reserve_s=5,
        )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_old_study_created_delegated_gt_max_replays(tmp_path, clock, runtime_cls):
    """Old StudyCreated with delegated_costly > max_cost_rub still reopens + finalizes.

    Exercises the real reopen path: ``open_runtime`` → ``_project_local`` →
    ``Study.model_validate`` (local_project). The ratio check lives only in
    ``validate_budget_for_create`` / ``create_study``, so replay must not raise.
    """
    from decimal import Decimal

    from coscientist.core.events.store import SqliteEventStore
    from coscientist.core.execution.domain import Budget, HitlPolicy, Study

    sid = "st_bg_old_ratio"
    root = tmp_path / sid
    root.mkdir()
    store = SqliteEventStore(sid, root / "events.db", clock=clock, owner="seed")
    await store.acquire_lease("seed")
    now = clock.now()
    # Raw journal row (pre-create_study check): looser delegated_costly than max_cost_rub.
    payload = {
        "study_id": sid,
        "profile": "research",
        "request": "q",
        "catalog_version": 3,
        "hitl_policy": HitlPolicy().model_dump(mode="json"),
        "deadline": (now + timedelta(seconds=60)).isoformat(),
        "created_at": now.isoformat(),
        "parent_study_id": None,
        "budget": {
            "max_llm_calls": 100,
            "max_tokens": 1_000_000,
            "max_cost_rub": "50",
            "max_tool_calls": 500,
            "deadline": None,
            "delegated_costly": "999",
        },
        "finalize_reserve_s": 5,
    }
    # Pin: fold path uses the same Study.model_validate as local_project.
    folded = Study.model_validate(payload)
    assert folded.budget is not None
    assert folded.budget.delegated_costly == Decimal("999")
    assert folded.budget.max_cost_rub == Decimal("50")
    # Budget alone also accepts the legacy ratio (no model_validator).
    Budget.model_validate(payload["budget"])
    await store.append(
        UnstampedEvent(
            study_id=sid,
            type="StudyCreated",
            entity_key=sid,
            idem_key=make_idem("StudyCreated", sid),
            payload=payload,
            ts=now,
            schema_version=1,
        )
    )
    await store.release_lease("seed", store.epoch)
    await store.close()

    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    assert rt.study is not None
    assert rt.study.budget is not None
    assert rt.study.budget.delegated_costly == Decimal("999")
    assert rt.study.budget.max_cost_rub == Decimal("50")
    await rt.ensure_lease(ttl_s=300)
    clock.advance(timedelta(seconds=120))
    await rt.tick()
    assert rt.view().phase == "closed"
    assert _fin(await rt.store.read_all()).payload["trigger"] == "deadline"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_all_done_budget_reasons_finalize_all_done(tmp_path, clock):
    """All tasks DONE(budget) without study flag → all_done, not budget trigger."""
    from coscientist.core.claims.reasons import CloseReason
    from coscientist.core.plan.cancel import _close_open_work

    sid = "st_bg3"
    rt = await _boot(tmp_path, clock, sid)
    events = _close_open_work(
        rt, reason=TaskReason.BUDGET, close=CloseReason.BUDGET
    )
    if events:
        await rt._append(events)
    assert rt.view().phase != "closed"
    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    await rt2.tick()
    assert _fin(await rt2.store.read_all()).payload["trigger"] == "all_done"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_finalize_zero_questions(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_zq")
    await rt.accept_plan([_spec("E1")], questions=[], revision=1)
    await _skip_all(rt)
    await rt.tick()
    assert _fin(await rt.store.read_all()).payload["question_dispositions"] == {}
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_report_task_failed_still_report(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_rf", seconds=120)
    _, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts
    for key in ("SPEC", "HYP", "LIT"):
        await record_success(rt, await rt.start_attempt(key), arts)
    await rt.accept_plan([])
    await rt.fail_attempt(await rt.start_attempt("REPORT"), retryable=False)
    await rt.tick()
    ev = _fin(await rt.store.read_all())
    assert ev.payload.get("report_artifact_id") or ev.payload.get("minimal_report")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_phase_finalizing_between_done_and_finalized(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_ph")
    await _skip_all(rt)
    assert rt.view().phase == "finalizing"
    await rt.finalize(trigger="all_done")
    assert rt.view().phase == "closed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_conclusion_per_question(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_cq")
    rt.artifacts = LocalArtifactStore(tmp_path / "arts")
    qs = [QuestionSpec(id="Q1", text="one"), QuestionSpec(id="Q2", text="two")]
    await rt.accept_plan([_spec("E1", ["Q1"]), _spec("E2", ["Q2"])], questions=qs)
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    ev = _fin(await rt.store.read_all())
    assert len(ev.payload["conclusion_ids"]) >= 2
    assert len(rt.graph.nodes_by_kind("conclusion")) >= 2
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_total_on_render_failure(tmp_path, clock, monkeypatch):
    rt = await _boot(tmp_path, clock, "st_rr")
    rt.artifacts = LocalArtifactStore(tmp_path / "arts")
    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "coscientist.core.finalize.finalize.render_markdown_report", _boom
    )
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    ev = _fin(await rt.store.read_all())
    assert ev.payload["minimal_report"] and ev.payload["fallback_reason"] == "RuntimeError"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_total_on_cas_failure(tmp_path, clock):
    from coscientist.core.events.errors import CASUnavailable

    class _BoomArts:
        """Realistic CAS failure: store attached but put raises CASUnavailable."""

        def put(self, *_a, **_k):
            raise CASUnavailable("cas unreachable")

    rt = await _boot(tmp_path, clock, "st_cas")
    boom = _BoomArts()
    rt.artifacts = boom
    if rt.executor is not None:
        rt.executor.artifacts = boom
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    ev = _fin(await rt.store.read_all())
    assert ev.payload["minimal_report"] and ev.payload["report_artifact_id"] is None
    assert ev.payload["fallback_reason"] == "CASUnavailable"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalized_dispositions_replay(tmp_path, clock):
    sid = "st_rep"
    rt = await _boot(tmp_path, clock, sid)
    await rt.accept_plan([_spec("E1", ["Q1"])], questions=[QuestionSpec(id="Q1", text="why")])
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    payload = _fin(await rt.store.read_all()).payload["question_dispositions"]
    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    assert rt2.view().phase == "closed"
    assert dispositions(tasks=rt2.tasks, plans=rt2.plans) == payload
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_finalize_idempotent(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_id")
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    await rt.finalize(trigger="cancel")
    fin = _fin(await rt.store.read_all())
    assert fin.payload["trigger"] == "all_done"
    await close_runtime(rt)


def _study_finalized_envelope(sid: str, clock, *, idem_key: str, payload: dict) -> UnstampedEvent:
    return UnstampedEvent(
        study_id=sid,
        type="StudyFinalized",
        entity_key=sid,
        idem_key=idem_key,
        payload=payload,
        ts=clock.now(),
        schema_version=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["append", "append_many"])
async def test_second_distinct_study_finalized_rejected(tmp_path, clock, via):
    """Exactly one StudyFinalized: a different idem_key after finalize is StudyClosedError."""
    sid = f"st_sf2_{via}"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    original = _fin(await rt.store.read_all())
    n_before = len(await rt.store.read_all())
    late_write_rejected_total.clear()
    distinct = _study_finalized_envelope(
        sid,
        clock,
        idem_key=f"StudyFinalized:{sid}:extra",
        payload=_fin_payload(trigger="cancel", study_id=sid),
    )
    with pytest.raises(StudyClosedError):
        if via == "append_many":
            await rt.store.append_many([distinct])
        else:
            await rt.store.append(distinct)
    assert late_write_rejected_total["StudyFinalized"] >= 1
    events = await rt.store.read_all()
    assert len(events) == n_before
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1 and fins[0].seq == original.seq
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_second_distinct_study_finalized_rejected_after_restart(tmp_path, clock):
    """Replay/reopen still enforces at-most-one StudyFinalized."""
    sid = "st_sf2_restart"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    original = _fin(await rt.store.read_all())
    await close_runtime(rt)

    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    assert rt2.view().phase == "closed"
    distinct = _study_finalized_envelope(
        sid,
        clock,
        idem_key=f"StudyFinalized:{sid}:after-restart",
        payload=_fin_payload(trigger="deadline", study_id=sid),
    )
    with pytest.raises(StudyClosedError):
        await rt2.store.append_many([distinct])
    fins = [e for e in await rt2.store.read_all() if e.type == "StudyFinalized"]
    assert len(fins) == 1 and fins[0].seq == original.seq
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_identical_finalize_retry_returns_original(tmp_path, clock):
    """Same idem_key+payload after finalize is idempotent (not StudyClosedError)."""
    sid = "st_sf_retry"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    payload = _fin_payload(study_id=sid)
    key = make_idem("StudyFinalized", sid)
    first = _study_finalized_envelope(sid, clock, idem_key=key, payload=payload)
    stored = (await rt.store.append_many([first]))[0]
    # Late non-finalize write rejected ⇒ study is closed (behavior, not cache).
    with pytest.raises(StudyClosedError):
        await rt.store.append(
            UnstampedEvent(
                study_id=sid,
                type="TaskDone",
                entity_key="late",
                idem_key=make_idem("TaskDone", "late"),
                payload={"task_id": "late", "outcome": "FAILED", "reason": "failed"},
                ts=clock.now(),
                schema_version=1,
            )
        )
    # Writer path (append_many): returns existing envelope.
    out = await rt.store.append_many(
        [_study_finalized_envelope(sid, clock, idem_key=key, payload=payload)]
    )
    assert len(out) == 1 and out[0].seq == stored.seq
    # Direct append: Idempotent with original seq (not StudyClosedError).
    with pytest.raises(Idempotent) as ei:
        await rt.store.append(
            _study_finalized_envelope(sid, clock, idem_key=key, payload=payload)
        )
    assert ei.value.seq == stored.seq
    # Same key, different payload → Conflict (genuinely different finalize).
    with pytest.raises(Conflict):
        await rt.store.append_many(
            [
                _study_finalized_envelope(
                    sid,
                    clock,
                    idem_key=key,
                    payload=_fin_payload(trigger="cancel", study_id=sid),
                )
            ]
        )
    _fin(await rt.store.read_all())
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_identical_finalize_retry_after_restart(tmp_path, clock):
    sid = "st_sf_retry_restart"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    payload = _fin_payload(study_id=sid)
    key = make_idem("StudyFinalized", sid)
    stored = (
        await rt.store.append_many(
            [_study_finalized_envelope(sid, clock, idem_key=key, payload=payload)]
        )
    )[0]
    await close_runtime(rt)

    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    out = await rt2.store.append_many(
        [_study_finalized_envelope(sid, clock, idem_key=key, payload=payload)]
    )
    assert out[0].seq == stored.seq
    _fin(await rt2.store.read_all())
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_append_many_two_study_finalized_rejects_second(tmp_path, clock):
    """B1: append_many([SF(k1), SF(k2)]) must not commit two StudyFinalized."""
    sid = "st_sf_batch2"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    # Warm path: probe open study once so a False probe cannot stick (only True cached).
    assert rt.view().phase != "closed"
    sf1 = _study_finalized_envelope(
        sid,
        clock,
        idem_key=f"StudyFinalized:{sid}:a",
        payload=_fin_payload(study_id=sid),
    )
    sf2 = _study_finalized_envelope(
        sid,
        clock,
        idem_key=f"StudyFinalized:{sid}:b",
        payload=_fin_payload(trigger="cancel", study_id=sid),
    )
    with pytest.raises(StudyClosedError):
        await rt.store.append_many([sf1, sf2])
    events = await rt.store.read_all()
    assert [e for e in events if e.type == "StudyFinalized"] == []
    # Single SF still works after the rolled-back batch.
    await rt.store.append_many([sf1])
    _fin(await rt.store.read_all())
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_append_many_study_finalized_then_other_rejects(tmp_path, clock):
    """B1: append_many([SF, TaskCreated]) must not write past finalize in-batch."""
    sid = "st_sf_batch_other"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    n_before = len(await rt.store.read_all())
    assert rt.view().phase != "closed"
    sf = _study_finalized_envelope(
        sid,
        clock,
        idem_key=make_idem("StudyFinalized", sid),
        payload=_fin_payload(study_id=sid),
    )
    late = UnstampedEvent(
        study_id=sid,
        type="TaskCreated",
        entity_key=f"{sid}/X",
        idem_key=make_idem("TaskCreated", sid, "X"),
        payload={
            "task_id": f"{sid}/X",
            "kind": "experiment",
            "spec": {
                "task_key": "X",
                "title": "x",
                "goal": "g",
                "route": "experiment",
            },
            "state": "PENDING",
            "deadline": clock.now().isoformat(),
        },
        ts=clock.now(),
        schema_version=1,
    )
    with pytest.raises(StudyClosedError):
        await rt.store.append_many([sf, late])
    events = await rt.store.read_all()
    assert len(events) == n_before
    assert not any(e.type == "StudyFinalized" for e in events)
    assert not any(e.type == "TaskCreated" and e.entity_key.endswith("/X") for e in events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_concurrent_finalize_exactly_one(tmp_path, clock):
    """Real race: Barrier + two threads/stores; exactly one SF; {ok, StudyClosedError}."""
    import threading

    from coscientist.core.events.store import SqliteEventStore

    sid = "st_sf_concurrent"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    db = rt.store.db_path
    await close_runtime(rt)

    store_a = SqliteEventStore(sid, db, clock=clock, owner="race", lease_ttl_s=120)
    await store_a.acquire_lease("race")
    store_b = SqliteEventStore(
        sid,
        db,
        clock=clock,
        owner=store_a.owner,
        epoch=store_a.epoch,
        owner_nonce=store_a.owner_nonce,
        lease_ttl_s=120,
    )

    barrier = threading.Barrier(2)
    slots: list[object | None] = [None, None]

    def _race(idx: int, store: SqliteEventStore, key: str, trigger: str) -> None:
        env = _study_finalized_envelope(
            sid,
            clock,
            idem_key=key,
            payload=_fin_payload(trigger=trigger, study_id=sid),
        )
        try:
            barrier.wait(timeout=5)
            with store._db_lock:  # noqa: SLF001 — sync path under store lock
                slots[idx] = store._append_many_sync([env])  # noqa: SLF001
        except BaseException as exc:  # noqa: BLE001 — capture race outcome
            slots[idx] = exc

    threads = [
        threading.Thread(
            target=_race,
            args=(0, store_a, f"StudyFinalized:{sid}:a", "all_done"),
        ),
        threading.Thread(
            target=_race,
            args=(1, store_b, f"StudyFinalized:{sid}:b", "deadline"),
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    kinds: set[str] = set()
    for outcome in slots:
        if isinstance(outcome, list):
            kinds.add("success")
        elif isinstance(outcome, StudyClosedError):
            kinds.add("StudyClosedError")
        else:
            raise AssertionError(f"unexpected race outcome: {type(outcome)!r} {outcome!r}")
    assert kinds == {"success", "StudyClosedError"}

    events = await store_a.read_all()
    fin = _fin(events)
    assert sum(1 for e in events if e.type == "StudyFinalized") == 1

    await store_a.release_lease(store_a.owner, store_a.epoch)
    await store_a.close()
    await store_b.close()

    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    assert rt2.view().phase == "closed"
    assert _fin(await rt2.store.read_all()).seq == fin.seq
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_legacy_two_study_finalized_fold_first_wins(tmp_path, clock):
    """Legacy journal with two SF: fold first-wins == store projections == rt2 graph."""
    import json

    from coscientist.core.events.digest import payload_digest
    from coscientist.core.events.projections import apply_event
    from coscientist.core.events.store import SqliteEventStore
    from coscientist.core.finalize.finalize_nodes import (
        conclusion_id_for_study,
        graph_rows_from_finalized,
    )

    sid = "st_sf_legacy_dup"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    # Distinct terminal_reason so first-wins graph synthesis can be distinguished
    # from a last-wins cancel payload.
    payload_a = _fin_payload(trigger="all_done", study_id=sid)
    payload_a["terminal_reason"] = "completed"
    payload_b = _fin_payload(trigger="cancel", study_id=sid)
    payload_b["terminal_reason"] = "cancelled"
    # Bypass store guard: insert two SF rows via raw SQL (legacy corruption).
    conn = rt.store._conn  # noqa: SLF001
    for key, payload in (
        (f"StudyFinalized:{sid}:legacy_a", payload_a),
        (f"StudyFinalized:{sid}:legacy_b", payload_b),
    ):
        conn.execute(
            "INSERT INTO events(study_id, type, entity_key, idem_key, payload, ts, "
            "schema_version, epoch, payload_digest) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                sid,
                "StudyFinalized",
                sid,
                key,
                json.dumps(payload, sort_keys=True),
                clock.now().isoformat(),
                1,
                rt.store.epoch,
                payload_digest(payload),
            ),
        )
    conn.commit()
    db = rt.store.db_path
    await close_runtime(rt)

    store = SqliteEventStore(sid, db, clock=clock)
    await store.acquire_lease("fold")
    events = await store.read_all()
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 2
    # Replay SF into projections (upcasted envelopes from read_all) — first-wins.
    for ev in fins:
        apply_event(store, ev)
    store._conn.commit()  # noqa: SLF001

    folded = fold_events(events)
    first_payload = folded["studies"][sid]["finalized_payload"]
    assert first_payload["trigger"] == "all_done"
    assert first_payload["terminal_reason"] == "completed"

    study_row = await store.get_row("studies", sid)
    assert study_row is not None and study_row.get("finalized") is True
    assert study_row["finalized_payload"] == first_payload
    meta_row = await store.get_row("meta", sid)
    assert meta_row is not None and meta_row.get("finalized") is True
    assert meta_row.get("duplicate_study_finalized") is True
    assert meta_row.get("tainted") is True

    fold_nodes = folded.get("graph_nodes") or {}
    fold_edges = folded.get("graph_edges") or {}
    assert fold_nodes and fold_edges
    proj_nodes = {
        r["node_id"]: r
        for r in await store.get_projection("graph_nodes")
        if isinstance(r, dict) and "node_id" in r
    }
    proj_edges = {
        str(r.get("edge_id") or f"{r.get('src_id')}:{r.get('dst_id')}:{r.get('kind')}"): r
        for r in await store.get_projection("graph_edges")
        if isinstance(r, dict)
    }
    # Bidirectional equivalence: fold ↔ store projections, both directions.
    assert set(fold_nodes) == set(proj_nodes)
    assert set(fold_edges) == set(proj_edges)
    for nid in set(fold_nodes) | set(proj_nodes):
        assert fold_nodes[nid] == proj_nodes[nid]
    for eid in set(fold_edges) | set(proj_edges):
        assert fold_edges[eid] == proj_edges[eid]

    await store.release_lease(store.owner, store.epoch)
    await store.close()

    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    assert rt2.view().phase == "closed"
    study2 = await rt2.store.get_row("studies", sid)
    assert study2 is not None
    assert study2["finalized_payload"] == first_payload
    assert study2["finalized_payload"]["trigger"] != "cancel"
    # rt2 graph must match first SF (not cancel/cancelled last-wins).
    expected_nodes, expected_edges = graph_rows_from_finalized(
        sid, first_payload, created_at=fins[0].ts
    )
    assert expected_nodes and expected_edges
    live_nodes = {n.node_id: n for n in rt2.graph.all_nodes()}
    live_edges = {e.edge_id: e for e in rt2.graph.all_edges()}
    assert set(expected_nodes) <= set(live_nodes)
    assert set(expected_edges) <= set(live_edges)
    scid = conclusion_id_for_study(sid)
    live_study = live_nodes[scid]
    synth = (live_study.attrs or {}).get("synthesis") or ""
    assert synth == expected_nodes[scid]["attrs"]["synthesis"]
    assert "terminal:completed" in synth
    assert "terminal:cancelled" not in synth
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_legacy_duplicate_study_finalized_taints_and_report(tmp_path, clock):
    """Second SF in a legacy journal taints meta + local fold (not the frozen report)."""
    import json

    from coscientist.core.events.digest import payload_digest
    from coscientist.core.events.projections import apply_event
    from coscientist.core.events.store import SqliteEventStore

    sid = "st_sf_legacy_taint"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    payload_a = _fin_payload(trigger="all_done", study_id=sid)
    payload_b = _fin_payload(trigger="cancel", study_id=sid)
    conn = rt.store._conn  # noqa: SLF001
    for key, payload in (
        (f"StudyFinalized:{sid}:a", payload_a),
        (f"StudyFinalized:{sid}:b", payload_b),
    ):
        conn.execute(
            "INSERT INTO events(study_id, type, entity_key, idem_key, payload, ts, "
            "schema_version, epoch, payload_digest) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                sid,
                "StudyFinalized",
                sid,
                key,
                json.dumps(payload, sort_keys=True),
                clock.now().isoformat(),
                1,
                rt.store.epoch,
                payload_digest(payload),
            ),
        )
    conn.commit()
    db = rt.store.db_path
    await close_runtime(rt)

    store = SqliteEventStore(sid, db, clock=clock)
    await store.acquire_lease("taint")
    events = await store.read_all()
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 2
    for ev in fins:
        apply_event(store, ev)
    store._conn.commit()  # noqa: SLF001

    meta = await store.get_row("meta", sid)
    assert meta is not None
    assert meta.get("duplicate_study_finalized") is True
    assert meta.get("tainted") is True
    folded = fold_events(events)
    assert folded["meta"][sid]["duplicate_study_finalized"] is True
    assert folded["meta"][sid]["tainted"] is True
    # Frozen first SF report is unchanged (finalize already returned / never re-runs).
    assert "internal_error" not in (fins[0].payload.get("contributing_reasons") or [])
    await store.release_lease(store.owner, store.epoch)
    await store.close()

    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    assert len([e for e in await rt2.store.read_all() if e.type == "StudyFinalized"]) == 2
    assert rt2._duplicate_study_finalized
    assert rt2._duplicate_study_finalized[0]["tainted"] is True
    assert rt2.view().phase == "closed"
    await close_runtime(rt2)

    # Replay-twice: second reopen still sees exactly two SF and the same taint mark.
    rt3 = await open_runtime(tmp_path, sid, clock=clock, owner="t3")
    assert len([e for e in await rt3.store.read_all() if e.type == "StudyFinalized"]) == 2
    assert rt3._duplicate_study_finalized
    assert rt3._duplicate_study_finalized[0]["tainted"] is True
    assert rt3.view().phase == "closed"
    meta3 = await rt3.store.get_row("meta", sid)
    assert meta3 is not None
    assert meta3.get("duplicate_study_finalized") is True
    assert meta3.get("tainted") is True
    await close_runtime(rt3)


@pytest.mark.asyncio
async def test_finalize_study_closed_with_event_is_success(
    tmp_path, clock, monkeypatch
):
    """StudyClosedError on append succeeds when StudyFinalized already exists."""
    sid = "st_sf_closed_ok"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    key = make_idem("StudyFinalized", sid)
    await rt.store.append_many(
        [
            _study_finalized_envelope(
                sid, clock, idem_key=key, payload=_fin_payload(study_id=sid)
            )
        ]
    )
    assert rt.view().phase != "closed"

    async def boom_closed(_events):
        raise StudyClosedError(sid, "StudyFinalized")

    monkeypatch.setattr(rt, "_append", boom_closed)
    await rt.finalize(trigger="cancel")
    assert rt.view().phase == "closed"
    _fin(await rt.store.read_all())
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_study_closed_without_event_reraises(
    tmp_path, clock, monkeypatch
):
    """StudyClosedError with no StudyFinalized in store must not mark finalized."""
    sid = "st_sf_closed_neg"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)

    async def boom_closed(_events):
        raise StudyClosedError(sid, "StudyFinalized")

    monkeypatch.setattr(rt, "_append", boom_closed)
    with pytest.raises(StudyClosedError):
        await rt.finalize(trigger="all_done")
    assert not any(e.type == "StudyFinalized" for e in await rt.store.read_all())
    assert rt.view().phase != "closed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_plan_refused_and_normal_finalize_exactly_one(tmp_path, clock):
    """PlanRefused path and normal finalize each leave exactly one StudyFinalized."""
    rt = await _boot(tmp_path, clock, "st_sf_plan")
    await rt.accept_plan(
        [_spec("E1", ["QX"])], questions=[QuestionSpec(id="Q1", text="ok")]
    )
    events = await rt.store.read_all()
    assert any(e.type == "PlanRefused" for e in events)
    _fin(events)
    with pytest.raises(StudyClosedError):
        await rt.store.append(
            _study_finalized_envelope(
                rt.study.study_id,
                clock,
                idem_key=f"StudyFinalized:{rt.study.study_id}:dup",
                payload=_fin_payload(study_id=rt.study.study_id),
            )
        )
    await close_runtime(rt)

    rt2 = await _boot(tmp_path / "n", clock, "st_sf_norm")
    await _skip_all(rt2)
    await rt2.finalize(trigger="all_done")
    _fin(await rt2.store.read_all())
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_internal_error_finalize_exactly_one(tmp_path, clock, monkeypatch):
    """Totality/fallback finalize still produces exactly one StudyFinalized."""
    rt = await _boot(tmp_path, clock, "st_sf_ie")
    rt.artifacts = LocalArtifactStore(tmp_path / "arts_ie")

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr("coscientist.core.finalize.finalize.render_markdown_report", _boom)
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    fin = _fin(await rt.store.read_all())
    assert fin.payload.get("fallback_reason") == "RuntimeError"
    with pytest.raises(StudyClosedError):
        await rt.store.append_many(
            [
                _study_finalized_envelope(
                    rt.study.study_id,
                    clock,
                    idem_key=f"StudyFinalized:{rt.study.study_id}:dup",
                    payload=_fin_payload(
                        trigger="internal_error", study_id=rt.study.study_id
                    ),
                )
            ]
        )
    _fin(await rt.store.read_all())
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_fetch_integrity_raised_before_finalize_then_guard(tmp_path, clock):
    """Open-time FetchIntegrityRaised (#26) still appends; after finalize store rejects it.

    Reconcile journals FIR while the study is open; termination leaves exactly one
    StudyFinalized; a late FIR (or second finalize) hits StudyClosedError.
    """
    sid = "st_sf_fir"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    fir = UnstampedEvent(
        study_id=sid,
        type="FetchIntegrityRaised",
        entity_key="call1",
        idem_key=make_idem("FetchIntegrityRaised", "call1", "papers_get"),
        payload={
            "kind": "fetch_tool_reclassified",
            "tool": "papers_get",
            "call_id": "call1",
            "internal_error": True,
        },
        ts=clock.now(),
        schema_version=1,
    )
    stored_fir = (await rt.store.append_many([fir]))[0]
    assert stored_fir.type == "FetchIntegrityRaised"
    await rt.finalize(trigger="all_done")
    events = await rt.store.read_all()
    fin = _fin(events)
    assert any(e.type == "FetchIntegrityRaised" for e in events)
    fin_seq = int(fin.seq or 0)
    assert int(stored_fir.seq or 0) < fin_seq
    # Late FIR (new idem_key) after StudyFinalized → StudyClosedError.
    with pytest.raises(StudyClosedError):
        await rt.store.append(
            UnstampedEvent(
                study_id=sid,
                type="FetchIntegrityRaised",
                entity_key="call2",
                idem_key=make_idem("FetchIntegrityRaised", "call2", "papers_get"),
                payload={
                    "kind": "fetch_tool_reclassified",
                    "tool": "papers_get",
                    "call_id": "call2",
                    "internal_error": True,
                },
                ts=clock.now(),
                schema_version=1,
            )
        )
    # Identical FIR retry is idempotent (same as SF: check before StudyClosedError).
    with pytest.raises(Idempotent) as ei:
        await rt.store.append(fir)
    assert ei.value.seq == stored_fir.seq
    _fin(await rt.store.read_all())
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_write_after_finalized_rejected_no_event(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_lw")
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    n_before = len(await rt.store.read_all())
    late_write_rejected_total.clear()
    tid = next(iter(rt.tasks))
    with pytest.raises(StudyClosedError):
        await rt.store.append(UnstampedEvent(
            study_id=rt.study.study_id, type="TaskDone", entity_key=tid,
            idem_key=make_idem("TaskDone", tid, "late"),
            payload={"task_id": tid, "outcome": "FAILED", "reason": "failed"},
            ts=clock.now(), schema_version=1,
        ))
    assert late_write_rejected_total["TaskDone"] >= 1
    assert len(await rt.store.read_all()) == n_before
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_p5_vs_study_deadline_same_tick(tmp_path, clock):
    """P5 (deadline < ed) runs before study-deadline close on the same tick."""
    rt = await _boot(tmp_path, clock, "st_p5", reserve=30, seconds=7200)
    sid = rt.study.study_id
    ed = rt.study.effective_deadline()
    assert rt.tasks[f"{sid}/SPEC"].deadline < ed
    assert rt.tasks[f"{sid}/REPORT"].deadline == ed
    await rt.ensure_lease(ttl_s=10_000)
    clock.advance(timedelta(seconds=7200))
    await rt.tick()
    for key in ("SPEC", "HYP", "LIT", "PLAN"):
        t = rt.tasks[f"{sid}/{key}"]
        assert t.state == TaskState.DONE
        assert t.outcome == TaskOutcome.FAILED
        assert t.reason == TaskReason.DEADLINE
    report = rt.tasks[f"{sid}/REPORT"]
    assert report.outcome == TaskOutcome.SKIPPED
    assert report.reason == TaskReason.STUDY_DEADLINE
    assert _fin(await rt.store.read_all()).payload["trigger"] == "deadline"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_p5_after_long_downtime_uses_task_deadline(tmp_path, clock):
    """After downtime past ed, tasks with own deadline < ed are FAILED(deadline)."""
    sid = "st_p5d"
    rt = await _boot(tmp_path, clock, sid, reserve=30, seconds=7200)
    await close_runtime(rt)
    clock.advance(timedelta(hours=48))
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    await rt.tick()
    spec = rt.tasks[f"{sid}/SPEC"]
    assert spec.outcome == TaskOutcome.FAILED and spec.reason == TaskReason.DEADLINE
    report = rt.tasks[f"{sid}/REPORT"]
    assert report.outcome == TaskOutcome.SKIPPED
    assert report.reason == TaskReason.STUDY_DEADLINE
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_rejects_unknown_address(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_ua")
    pid = await rt.accept_plan([_spec("E1", ["QX"])], questions=[QuestionSpec(id="Q1", text="ok")])
    assert pid == ""
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "PlanRefused"]
    assert refused and "unknown question" in str(refused[0].payload.get("detail") or "")
    _fin(events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_rejects_question_text_conflict(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_qc")
    await rt.accept_plan([], questions=[QuestionSpec(id="Q1", text="one")], revision=1)
    pid = await rt.accept_plan([], questions=[QuestionSpec(id="Q1", text="other")], revision=2)
    assert pid == ""
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "PlanRefused"]
    assert refused and "text conflict" in str(refused[0].payload.get("detail") or "")
    _fin(events)
    await close_runtime(rt)


def _fin_payload(*, trigger: str = "all_done", study_id: str = "st") -> dict:
    """Store-level StudyFinalized payload that satisfies _fin asserts."""
    return {
        "trigger": trigger,
        "terminal_reason": "completed",
        "contributing_reasons": [],
        "verdict": "partial",
        "question_dispositions": {},
        "report_artifact_id": None,
        "conclusion_ids": conclusion_ids_for(study_id, {}),
        "minimal_report": {
            "study_id": study_id,
            "terminal_reason": "completed",
            "trigger": trigger,
            "contributing_reasons": [],
            "summary": "store-test",
            "fallback_reason": "test",
            "verdict": "partial",
            "tasks": [],
            "questions": [],
            "hypotheses": [],
            "sections": {"real": [], "stub": [], "human": []},
            "truncated": True,
        },
        "fallback_reason": "test",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["append_many", "append"])
async def test_finalized_rollback_allows_retry_then_closes(tmp_path, clock, monkeypatch, via):
    """Failed StudyFinalized insert rolls back; retry succeeds; then late writes fail."""
    sid = f"st_rb_{via}"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    store = rt.store
    real_apply = store._apply_projection

    def boom_after_insert(ev: UnstampedEvent) -> None:
        real_apply(ev)
        if ev.type == "StudyFinalized":
            raise RuntimeError("after_insert_before_commit")

    monkeypatch.setattr(store, "_apply_projection", boom_after_insert)
    fin_ev = UnstampedEvent(
        study_id=sid,
        type="StudyFinalized",
        entity_key=sid,
        idem_key=make_idem("StudyFinalized", sid),
        payload=_fin_payload(study_id=sid),
        ts=clock.now(),
        schema_version=1,
    )
    with pytest.raises(RuntimeError, match="after_insert_before_commit"):
        if via == "append_many":
            await store.append_many([fin_ev])
        else:
            await store.append(fin_ev)
    assert not any(e.type == "StudyFinalized" for e in await store.read_all())
    monkeypatch.setattr(store, "_apply_projection", real_apply)
    if via == "append_many":
        await store.append_many([fin_ev])
    else:
        await store.append(fin_ev)
    _fin(await store.read_all())
    with pytest.raises(StudyClosedError):
        await store.append(
            UnstampedEvent(
                study_id=sid,
                type="TaskDone",
                entity_key="x",
                idem_key=make_idem("TaskDone", "x", "late"),
                payload={"task_id": "x", "outcome": "FAILED", "reason": "failed"},
                ts=clock.now(),
                schema_version=1,
            )
        )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_lease_reacquire_sees_peer_finalize(tmp_path, clock):
    """After peer finalizes, reacquire rejects late writes (I3)."""
    from coscientist.core.events.store import SqliteEventStore

    sid = "st_lease_cache"
    root = tmp_path / sid
    root.mkdir()
    db = root / "events.db"
    store_a = SqliteEventStore(sid, db, clock=clock, owner="a", lease_ttl_s=30)
    await store_a.acquire_lease("a")
    # Open study: no finalized projection yet (public tables, not private cache).
    assert await store_a.get_row("meta", sid) is None
    study_open = await store_a.get_row("studies", sid)
    assert study_open is None or not study_open.get("finalized")

    # Expire A's lease; B acquires and finalizes.
    clock.advance(timedelta(seconds=31))
    store_b = SqliteEventStore(sid, db, clock=clock, owner="b", lease_ttl_s=30)
    await store_b.acquire_lease("b")
    await store_b.append(
        UnstampedEvent(
            study_id=sid,
            type="StudyFinalized",
            entity_key=sid,
            idem_key=make_idem("StudyFinalized", sid),
            payload=_fin_payload(study_id=sid),
            ts=clock.now(),
            schema_version=1,
        )
    )
    await store_b.release_lease("b", store_b.epoch)
    await store_b.close()

    clock.advance(timedelta(seconds=31))
    await store_a.acquire_lease("a")
    with pytest.raises(StudyClosedError):
        await store_a.append(
            UnstampedEvent(
                study_id=sid,
                type="TaskDone",
                entity_key="late",
                idem_key=make_idem("TaskDone", "late"),
                payload={"task_id": "late", "outcome": "FAILED", "reason": "failed"},
                ts=clock.now(),
                schema_version=1,
            )
        )
    study_closed = await store_a.get_row("studies", sid)
    assert study_closed is not None and study_closed.get("finalized") is True
    await store_a.close()
