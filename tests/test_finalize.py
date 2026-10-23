"""PR 2: study finalizer, dispositions, minimal_report, upcasts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from coscientist.core.artifacts import LocalArtifactStore
from coscientist.core.digest import canonical_json
from coscientist.core.dispositions import dispositions
from coscientist.core.domain import (
    QuestionSpec,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.errors import StudyClosedError
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.finalize import (
    contributing_reasons_ordered,
    pick_finalize_trigger,
    terminal_reason_for,
)
from coscientist.core.metrics import late_write_rejected_total
from coscientist.core.minimal_report import MAX_BYTES, build_minimal_report
from coscientist.core.question_tasks import question_tasks, questions_from_spec_output
from coscientist.core.reasons import NotAttemptedReason, TaskReason, TerminalReason
from coscientist.core.runtime_factory import close_runtime, open_runtime
from coscientist.core.study_view import study_view
from coscientist.core.upcasters import upcast
from tests.helpers import attach_executor, record_success

_TS = datetime.fromisoformat("2026-01-01T00:00:00+00:00")


async def _boot(tmp: Path, clock, sid: str, *, reserve: int = 5, seconds: int = 60):
    rt = await open_runtime(tmp, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="research", request="q", catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=seconds), sid=sid,
        finalize_reserve_s=reserve,
    )
    return rt


async def _skip_all(rt, reason=TaskReason.CANCELLED):
    for t in list(rt.tasks.values()):
        if t.state != TaskState.DONE:
            await rt.cancel_task(t.spec.task_key, reason=reason)


def _fin(events):
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1
    return fins[0]


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
        return EventEnvelope(
            study_id="s", type="TaskDone", entity_key=key, idem_key=key,
            payload={"reason": reason}, ts=_TS, seq=seq,
        )
    assert contributing_reasons_ordered([td("a", "budget", 2), td("b", "cancelled", 1), td("c", "budget", 3)]) == [
        "cancelled", "budget",
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
        "questions": [{"id": f"Q{i}", "text": "t"} for i in (1, 2, 3)],
        "tasks": [
            {"task_key": "A", "addresses": ["Q1"]},
            {"task_key": "B", "addresses": ["Q2"]},
            {"task_key": "C", "addresses": ["Q3"]},
        ],
    }}
    tasks = {
        "st/A": _task("A", outcome=TaskOutcome.FAILED, reason=TaskReason.FAILED),
        "st/B": _task("B", outcome=TaskOutcome.SKIPPED, reason=TaskReason.CANCELLED),
        "st/C": _task("C"),
    }
    d = dispositions(tasks=tasks, plans=plans)
    assert d["Q1"]["reason"] == NotAttemptedReason.TASKS_FAILED.value
    assert d["Q2"]["reason"] == NotAttemptedReason.TASKS_SKIPPED.value
    assert d["Q3"]["reason"] == NotAttemptedReason.NO_COUNTING_CLAIMS.value


def test_questions_from_spec_output_wired():
    out = questions_from_spec_output(
        {"questions": [{"id": "Q1", "text": "one"}, {"formulation": "two"}, "three"]}
    )
    assert out == [{"id": "Q1", "text": "one"}, {"id": "Q2", "text": "two"}, {"id": "Q3", "text": "three"}]


def test_upcast_taskcreated_addresses_default():
    v, p = upcast("TaskCreated", 2, {"task_id": "t", "spec": {"task_key": "E1"}})
    assert v == 3 and p["spec"]["addresses"] == []


def test_upcast_planaccepted_questions_default():
    v, p = upcast("PlanAccepted", 1, {"plan_id": "p", "tasks": [{"task_key": "E1"}]})
    assert v == 2 and p["questions"] == [] and p["tasks"][0]["addresses"] == []


def test_upcast_study_finalized_v1_adds_counts_and_refusal():
    v, p = upcast(
        "StudyFinalized",
        1,
        {
            "trigger": "all_done",
            "terminal_reason": "completed",
            "contributing_reasons": [],
            "verdict": "partial",
            "question_dispositions": {},
            "report_artifact_id": None,
            "conclusion_ids": [],
            "minimal_report": {
                "study_id": "st",
                "terminal_reason": "completed",
                "trigger": "all_done",
                "contributing_reasons": [],
                "summary": "x",
                "fallback_reason": None,
                "verdict": "partial",
                "tasks": [],
                "questions": [
                    {
                        "question_id": "Q1",
                        "formulation": "q",
                        "disposition": "not_attempted",
                        "reason": "no_task",
                    }
                ],
                "sections": {"real": [], "stub": [], "human": []},
                "truncated": False,
            },
            "fallback_reason": None,
        },
    )
    assert v == 2
    assert p["refusal"] is None
    assert p["minimal_report"]["question_count"] == 1
    assert p["minimal_report"]["not_attempted_count"] == 1
    assert p["minimal_report"]["refusal"] is None


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
async def test_task_budget_does_not_close_study(tmp_path, clock):
    """TaskDone(budget) from a costly route must not close the whole study."""
    from coscientist.core.reasons import CloseReason

    sid = "st_bg2"
    rt = await _boot(tmp_path, clock, sid)
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
    events: list[EventEnvelope] = []
    for aid in a.attempts:
        if not rt.attempts.get(aid, {}).get("closed"):
            events.append(
                EventEnvelope(
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
                    schema_version=2,
                )
            )
    events.append(
        EventEnvelope(
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
            schema_version=2,
        )
    )
    await rt._append(events)
    await rt.tick()
    assert not rt._finalized
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
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
    )
    await rt.note_budget_exceeded()
    assert rt._budget_exceeded
    assert any(e.type == "BudgetExceeded" for e in await rt.store.read_all())
    assert not rt._finalized
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

    from coscientist.core.domain import Budget

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
        catalog_version="0.1.0",
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

    from coscientist.core.domain import Budget

    sid = "st_bg_bad"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    with pytest.raises(ValueError, match="delegated_costly"):
        await rt.create_study(
            profile="research",
            request="q",
            catalog_version="0.1.0",
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
    """Old StudyCreated with delegated_costly > max_cost_rub still reopens + finalizes."""
    from decimal import Decimal

    from coscientist.core.domain import HitlPolicy
    from coscientist.core.store import SqliteEventStore

    sid = "st_bg_old_ratio"
    root = tmp_path / sid
    root.mkdir()
    store = SqliteEventStore(sid, root / "events.db", clock=clock, owner="seed")
    await store.acquire_lease("seed")
    now = clock.now()
    # Raw journal row (pre-create_study check): looser delegated_costly than max_cost_rub.
    await store.append(
        EventEnvelope(
            study_id=sid,
            type="StudyCreated",
            entity_key=sid,
            idem_key=make_idem("StudyCreated", sid),
            payload={
                "study_id": sid,
                "profile": "research",
                "request": "q",
                "catalog_version": "0.1.0",
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
            },
            ts=now,
            schema_version=2,
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
    assert rt._finalized
    assert _fin(await rt.store.read_all()).payload["trigger"] == "deadline"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_all_done_budget_reasons_finalize_all_done(tmp_path, clock):
    """All tasks DONE(budget) without study flag → all_done, not budget trigger."""
    from coscientist.core.cancel import _close_open_work
    from coscientist.core.reasons import CloseReason

    sid = "st_bg3"
    rt = await _boot(tmp_path, clock, sid)
    events = _close_open_work(
        rt, reason=TaskReason.BUDGET, close=CloseReason.BUDGET
    )
    if events:
        await rt._append(events)
    assert not rt._finalized
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
    assert not rt._finalized and rt.view().phase == "finalizing"
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
        "coscientist.core.finalize.render_markdown_report", _boom
    )
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    ev = _fin(await rt.store.read_all())
    assert ev.payload["minimal_report"] and ev.payload["fallback_reason"] == "RuntimeError"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_total_on_cas_failure(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_cas")
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
    assert rt2._finalized and rt2.view().phase == "closed"
    assert dispositions(tasks=rt2.tasks, plans=rt2.plans) == payload
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_finalize_idempotent(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_id")
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    await rt.finalize(trigger="cancel")
    fins = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fins) == 1 and fins[0].payload["trigger"] == "all_done"
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
        await rt.store.append(EventEnvelope(
            study_id=rt.study.study_id, type="TaskDone", entity_key=tid,
            idem_key=make_idem("TaskDone", tid, "late"),
            payload={"task_id": tid, "outcome": "FAILED", "reason": "failed"},
            ts=clock.now(), schema_version=2,
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
    with pytest.raises(ValueError, match="unknown question"):
        await rt.accept_plan(
            [_spec("E1", ["QX"])], questions=[QuestionSpec(id="Q1", text="ok")]
        )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_rejects_question_text_conflict(tmp_path, clock):
    rt = await _boot(tmp_path, clock, "st_qc")
    await rt.accept_plan([], questions=[QuestionSpec(id="Q1", text="one")], revision=1)
    with pytest.raises(ValueError, match="text conflict"):
        await rt.accept_plan(
            [], questions=[QuestionSpec(id="Q1", text="other")], revision=2
        )
    await close_runtime(rt)


def _fin_payload(*, trigger: str = "all_done") -> dict:
    return {
        "trigger": trigger,
        "terminal_reason": "completed",
        "contributing_reasons": [],
        "verdict": "partial",
        "question_dispositions": {},
        "report_artifact_id": None,
        "conclusion_ids": [],
        "minimal_report": None,
        "fallback_reason": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["append_many", "append"])
async def test_finalized_cache_not_set_on_rollback(tmp_path, clock, monkeypatch, via):
    """StudyFinalized insert + rollback must not leave _finalized_cached True."""
    sid = f"st_rb_{via}"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    store = rt.store
    assert store._finalized_cached is not True
    real_apply = store._apply_projection

    def boom_after_insert(ev: EventEnvelope) -> None:
        real_apply(ev)
        if ev.type == "StudyFinalized":
            raise RuntimeError("after_insert_before_commit")

    monkeypatch.setattr(store, "_apply_projection", boom_after_insert)
    fin_ev = EventEnvelope(
        study_id=sid,
        type="StudyFinalized",
        entity_key=sid,
        idem_key=make_idem("StudyFinalized", sid),
        payload=_fin_payload(),
        ts=clock.now(),
        schema_version=1,
    )
    with pytest.raises(RuntimeError, match="after_insert_before_commit"):
        if via == "append_many":
            await store.append_many([fin_ev])
        else:
            await store.append(fin_ev)
    assert store._finalized_cached is not True
    monkeypatch.setattr(store, "_apply_projection", real_apply)
    if via == "append_many":
        await store.append_many([fin_ev])
    else:
        await store.append(fin_ev)
    assert store._finalized_cached is True
    with pytest.raises(StudyClosedError):
        await store.append(
            EventEnvelope(
                study_id=sid,
                type="TaskDone",
                entity_key="x",
                idem_key=make_idem("TaskDone", "x", "late"),
                payload={"task_id": "x", "outcome": "FAILED", "reason": "failed"},
                ts=clock.now(),
                schema_version=2,
            )
        )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalized_cache_reset_on_lease_reacquire(tmp_path, clock):
    """Stale False on store A must clear on reacquire after B finalizes (I3)."""
    from coscientist.core.store import SqliteEventStore

    sid = "st_lease_cache"
    root = tmp_path / sid
    root.mkdir()
    db = root / "events.db"
    store_a = SqliteEventStore(sid, db, clock=clock, owner="a", lease_ttl_s=30)
    await store_a.acquire_lease("a")
    assert store_a._study_is_finalized() is False
    assert store_a._finalized_cached is False

    # Expire A's lease; B acquires and finalizes.
    clock.advance(timedelta(seconds=31))
    store_b = SqliteEventStore(sid, db, clock=clock, owner="b", lease_ttl_s=30)
    await store_b.acquire_lease("b")
    await store_b.append(
        EventEnvelope(
            study_id=sid,
            type="StudyFinalized",
            entity_key=sid,
            idem_key=make_idem("StudyFinalized", sid),
            payload={
                "trigger": "all_done",
                "terminal_reason": "completed",
                "contributing_reasons": [],
                "verdict": "partial",
                "question_dispositions": {},
                "report_artifact_id": None,
                "conclusion_ids": [],
                "minimal_report": None,
                "fallback_reason": None,
            },
            ts=clock.now(),
            schema_version=1,
        )
    )
    assert store_b._finalized_cached is True
    await store_b.release_lease("b", store_b.epoch)
    await store_b.close()

    # A still holds stale False until acquire_lease clears it.
    assert store_a._finalized_cached is False
    clock.advance(timedelta(seconds=31))
    await store_a.acquire_lease("a")
    assert store_a._finalized_cached is None
    with pytest.raises(StudyClosedError):
        await store_a.append(
            EventEnvelope(
                study_id=sid,
                type="TaskDone",
                entity_key="late",
                idem_key=make_idem("TaskDone", "late"),
                payload={"task_id": "late", "outcome": "FAILED", "reason": "failed"},
                ts=clock.now(),
                schema_version=2,
            )
        )
    assert store_a._finalized_cached is True
    await store_a.close()
