"""Adaptive plan runtime: expanded tasks are REALLY dispatched by the run loop.

Covers the minimal honest slice of ``lab/adaptive-plan-p1``:

- an executor ``expansion_request`` on a final result queues PLAN-n; the run
  loop dispatches PLAN-n itself (no second runner); the deterministic critic
  gate applies the request; the ADDED task goes through the SAME shared
  dispatch pass, budgets and limits;
- the shared session budget counts expansion work (replan + added task);
- an expanded coder task without an exact ``--coder-jobs`` binding for its
  task key refuses typed (no positional reuse of original jobs);
- replay after close folds to the same state and offers nothing to dispatch
  (dispatch is a pure function of journal state, live == replay).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coscientist.agents.schemas import (
    ExecutorRouteOut,
    QuestionIntent,
    TaskIntent,
    TaskSpecDraft,
    ToolArg,
)
from coscientist.cli.errors import CliRefuse
from coscientist.cli.run import _drive_plan_attempts
from coscientist.core.claims.rubric import freeze_rubric
from coscientist.core.claims.rubric_types import Rubric, RubricItem
from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.domain import (
    Budget,
    ExpansionRequest,
    OutputSpec,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.execution.runtime_factory import close_runtime, open_runtime
from tests.agents.test_adapter_roles import _exec_out_json
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.cli.test_run_study import (
    _intent_json,
    _payloads,
    _read_events,
    _run,
    _spec_json,
)
from tests.llm.test_client import _ok_body

_STUDY = "adaptive_study"


def _one_task_plan_json() -> str:
    """Planner cassette: ONE experiment task (EXP-0) + one question (q1)."""
    return _intent_json(
        (
            TaskIntent(
                question_indices=(1,),
                goal="overview",
                route="experiment",
                key="EXP-0",
                tools_hint=("static:dataset_overview",),
            ),
        ),
        questions=(QuestionIntent(final_type="artifact", text="what follows?"),),
    )


def _artifact_rubric() -> Rubric:
    """The same auto-attached rubric derive gives every tools_hint output."""
    return freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id="exists",
                    text="required artifact exists",
                    required=True,
                    check="code",
                    code_kind="artifact_exists",
                ),
            ],
            aggregate="all_required",
            k=None,
        )
    )


def _expansion_request() -> ExpansionRequest:
    """Valid typed request: deliverable output + catalog criterion + known deps."""
    return ExpansionRequest(
        tasks=[
            TaskSpec(
                task_key="agent_wants_followup",
                title="Follow-up experiment",
                goal="follow-up on the EXP-0 outcome",
                route="experiment",
                depends_on=["EXP-0"],
                addresses=["q1"],
                expected_outputs=[
                    OutputSpec(
                        name="main",
                        media_type="application/json",
                        required=True,
                        rubric=_artifact_rubric(),
                    )
                ],
                criteria=[RequirementRef(template_id="artifact_present")],
            )
        ],
        reason="need follow-up on observed result",
    )


def _exec_out_with_expansion_json(*, tool: str) -> str:
    out = ExecutorRouteOut.model_validate_json(_exec_out_json(tool=tool))
    return out.model_copy(update={"expansion_request": _expansion_request()}).model_dump_json()


def _replan_out_json() -> str:
    """PLAN-n executor output: the request is the sole apply input (no seed)."""
    return ExecutorRouteOut(
        summary="replan",
        values=(),
        tool_found=(),
        expansion_request=_expansion_request(),
    ).model_dump_json()


def _plain_followup_out_json() -> str:
    """Executor output for the ADDED task: values['main'] materializes the output."""
    return ExecutorRouteOut(
        summary="follow-up done",
        values=(ToolArg(key="main", value_json='{"ok": true}'),),
        tool_found=(),
    ).model_dump_json()


def _adaptive_bodies() -> list[bytes]:
    """tz_spec → planner → EXP-0 (request) → PLAN-1 (request) → EXP-1 (plain)."""
    return [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_one_task_plan_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(
            content=_exec_out_with_expansion_json(tool="dataset_overview"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
        _ok_body(content=_replan_out_json(), prompt_tokens=4, completion_tokens=2),
        _ok_body(content=_plain_followup_out_json(), prompt_tokens=4, completion_tokens=2),
    ]


@pytest.mark.asyncio
async def test_expanded_task_dispatched_and_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """expansion_request → PLAN-1 dispatched → PlanExpanded → EXP-1 really runs."""
    code, summary, _router = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=_adaptive_bodies(),
        study_id=_STUDY,
    )
    assert code == 0, summary
    assert summary["phase"] == "closed"
    assert summary["verdict"] == "fulfilled"
    events = await _read_events(tmp_path / "data", _STUDY)
    expanded = _payloads(events, "PlanExpanded")
    assert len(expanded) == 1 and expanded[0]["plan_rev"] == 2
    # The added task was really created, dispatched and closed SUCCESS.
    created = [p for p in _payloads(events, "TaskCreated") if str((p.get("spec") or {}).get("task_key")) == "EXP-1"]
    assert created, "expanded task EXP-1 must be created"
    exp1_results = [
        p
        for p in _payloads(events, "ResultRecorded")
        if str(p.get("task_id", "")).endswith("EXP-1") and p.get("outcome") == "SUCCESS"
    ]
    assert exp1_results, "expanded task EXP-1 must record a SUCCESS result"
    # Exactly one PLAN container, applied once — no recursive PLAN-n.
    plan_keys = [
        str((p.get("spec") or {}).get("task_key"))
        for p in _payloads(events, "TaskCreated")
        if str((p.get("spec") or {}).get("task_key", "")).startswith("PLAN-")
    ]
    assert plan_keys == ["PLAN-1"]
    # Shared dispatch: every LLM call went through the ONE session usage list
    # (rows with prompt_tokens are billed adapter runs; skip notes carry none).
    billed = [str(u["role"]) for u in summary["usage"] if "prompt_tokens" in u]
    assert billed == ["tz_spec", "planner", "executor-route", "executor-route", "executor-route"]
    assert summary["quality"]["exp_tasks"] == 2


@pytest.mark.asyncio
async def test_expanded_dispatch_shares_session_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_llm_calls covers replan but NOT the added task → typed budget refuse.

    Four calls fit (tz_spec, planner, EXP-0, PLAN-1); the EXPANDED task's
    attempt is the one that hits the shared Budget — expansion work draws the
    same session budget, not a parallel allowance.
    """
    with pytest.raises(CliRefuse) as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=_adaptive_bodies(),
            study_id="adaptive_budget",
            max_llm_calls=4,
        )
    assert ei.value.code == 4
    assert "budget" in str(ei.value)
    events = await _read_events(tmp_path / "data", "adaptive_budget")
    # PLAN-1 ran (4th call) and applied the expansion before the refuse.
    assert any(e.type == "PlanExpanded" for e in events)
    added = [
        e
        for e in events
        if e.type == "TaskCreated"
        and isinstance(e.payload, dict)
        and str((e.payload.get("spec") or {}).get("task_key")) == "EXP-1"
    ]
    assert added
    # The added task never recorded a result: the budget stop precedes it.
    assert not any(
        e.type == "ResultRecorded"
        and isinstance(e.payload, dict)
        and str(e.payload.get("task_id", "")).endswith("EXP-1")
        for e in events
    )


@pytest.mark.asyncio
async def test_expanded_coder_task_without_binding_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expanded coder task with no exact --coder-jobs binding → typed refuse."""
    coder_req = _expansion_request().model_copy(
        update={
            "tasks": [
                TaskSpec(
                    task_key="agent_wants_code",
                    title="Follow-up code run",
                    goal="code execution follow-up",
                    route="experiment",
                    depends_on=["EXP-0"],
                    addresses=["q1"],
                    tools_hint=["coder"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                )
            ]
        }
    )
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_one_task_plan_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(
            content=ExecutorRouteOut.model_validate_json(_exec_out_json(tool="dataset_overview"))
            .model_copy(update={"expansion_request": coder_req})
            .model_dump_json(),
            prompt_tokens=4,
            completion_tokens=2,
        ),
        _ok_body(
            content=ExecutorRouteOut(
                summary="replan",
                values=(),
                tool_found=(),
                expansion_request=coder_req,
            ).model_dump_json(),
            prompt_tokens=4,
            completion_tokens=2,
        ),
    ]
    with pytest.raises(CliRefuse) as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            study_id="adaptive_coder",
        )
    assert ei.value.code == 3
    assert "positional job reuse" in str(ei.value)
    events = await _read_events(tmp_path / "data", "adaptive_coder")
    assert any(e.type == "PlanExpanded" for e in events)
    # The refusal is typed and honest: no invented job, no attempt on the task.
    assert not any(
        e.type == "AttemptStarted"
        and isinstance(e.payload, dict)
        and str(e.payload.get("task_id", "")).endswith("EXP-1")
        for e in events
    )


@pytest.mark.asyncio
async def test_replayed_journal_offers_no_redispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay folds to the same state: nothing dispatchable, every attempt closed."""
    code, _summary, router = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=_adaptive_bodies(),
        study_id="adaptive_replay",
    )
    assert code == 0
    rt = await open_runtime(
        tmp_path / "data",
        "adaptive_replay",
        catalog=minimal_test_catalog(),
        clock=FakeClock(),
        owner="replay-reader",
        acquire=False,
    )
    try:
        assert rt.view().phase == "closed"
        assert rt.iter_dispatchable_tasks() == []
        assert all(row.get("closed") is True for row in rt.attempts.values() if isinstance(row, dict))
        plan_n = [t for t in rt.tasks.values() if t.spec.task_key.startswith("PLAN-")]
        assert [t.spec.task_key for t in plan_n] == ["PLAN-1"]
        # Driving the closed journal is a no-op (dispatch is journal-derived).
        executed_before = len(rt.attempts)
        await _drive_plan_attempts(
            rt,
            plan_out=_replay_plan_out(),
            tools=router,
            adapter=_NoopAdapter(),
            study_id="adaptive_replay",
            registered_inputs=(),
            role_budget=_never_budget(),
            usages=[],
            allowed_tool_names=frozenset({"static:dataset_overview"}),
            executed_aids=set(),
            coder_dispatched=set(),
            coder_backend=None,
            coder_jobs=None,
            writer_journal=_NullJournal(),  # type: ignore[arg-type]
            wall=FakeClock(),
            hitl_policy=_NullPolicy(),  # type: ignore[arg-type]
            deadline=rt.study.deadline,  # type: ignore[union-attr]
        )
        assert len(rt.attempts) == executed_before
    finally:
        await close_runtime(rt)


def _replay_plan_out() -> object:
    """Initial accepted plan shape (EXP-0 only) for the closed-journal drive."""
    return type(
        "_PlanOut",
        (),
        {
            "tasks": (
                TaskSpecDraft(
                    task_key="EXP-0",
                    title="overview",
                    goal="overview",
                    route="experiment",
                    depends_on=(),
                    addresses=("q1",),
                    tools_hint=("static:dataset_overview",),
                    expected_outputs=(),
                    criteria=(),
                ),
            ),
        },
    )()


class _NoopAdapter:
    def bind_tool_executor(self, _e: object) -> None:
        return None


class _NullJournal:
    def record(self, _ev: object) -> None:
        return None

    async def drain(self, *, strict: bool) -> None:
        return None


class _NullPolicy:
    pass


def _never_budget() -> Budget:
    return Budget(max_llm_calls=1, max_tokens=1, max_cost_rub=None, max_tool_calls=0)
