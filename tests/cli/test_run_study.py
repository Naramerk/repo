"""CLI ``run_study`` — honest journal cycle; usage written; negatives refuse."""

from __future__ import annotations

import argparse
import io
import json
import random
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import pytest

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.prompts.roles import (
    ROLE_EXECUTOR_REF,
    ROLE_PLANNER_REF,
    ROLE_TZ_SPEC_REF,
    role_prompt_registry,
)
from coscientist.agents.research_spec import Constraint, Goal, ResearchSpec, SuccessMetric
from coscientist.agents.schemas import (
    PlanIntent,
    QuestionIntent,
    RubricDraft,
    RubricItemDraft,
    TaskIntent,
    TaskSpecDraft,
)
from coscientist.checks.catalog import CATALOG_VERSION
from coscientist.cli.errors import CliRefuse
from coscientist.cli.http_transport import HttpxTransport
from coscientist.cli.llm_clock import WallLlmClock, wall_sleep, wall_wait_for
from coscientist.cli.run import _execute_executor_attempt, main, run_study
from coscientist.cli.static_router import StaticToolRouter
from coscientist.core.events.clock import FakeClock
from coscientist.core.events.events import StoredEvent
from coscientist.core.execution.domain import Budget, HitlPolicy, Need
from coscientist.llm.client import LlmClient
from coscientist.llm.errors import LlmTransportError
from coscientist.profiles.load import load_profile
from coscientist.tools.router import ToolNeed
from tests.agents.test_adapter_roles import (
    CassetteTransport,
    _exec_out_json,
)
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import close_runtime as _close_runtime_helper
from tests.helpers import open_runtime as _open_runtime_helper
from tests.llm.test_client import FakeClock as LlmFakeClock
from tests.llm.test_client import _config, _ok_body

_RESEARCH_PROFILE = load_profile("research", expected_tool_catalog_version=2)
_RESEARCH_MODEL = _RESEARCH_PROFILE.roles["planner"].model

_T = TypeVar("_T")


def _spec_json() -> str:
    return ResearchSpec(
        object="two_tasks study",
        goal=Goal(statement="complete two experiment tasks", research_form=None, trl=None),
        scope="synthetic",
        constraints=(Constraint(subtype="resource", content="cassette LLM", limit=None),),
        success_metrics=(SuccessMetric(metric="tasks", op="==", target="2", check="code", tolerance=None),),
    ).model_dump_json()


def _artifact_rubric() -> RubricDraft:
    return RubricDraft(
        items=(
            RubricItemDraft(
                id="exists",
                text="required artifact exists",
                required=True,
                check="code",
                code_kind="artifact_exists",
            ),
        ),
        aggregate="all_required",
        k=None,
    )


_NEED_OFF = Need(kind="need", needed=False, reason="off")


def _intent_json(
    tasks: tuple[TaskIntent, ...],
    *,
    hypotheses: Need = _NEED_OFF,
    literature: Need = _NEED_OFF,
    questions: tuple[QuestionIntent, ...] = (QuestionIntent(final_type="artifact", text="two tasks?"),),
) -> str:
    """Planner cassette body: typed PlanIntent (frozen-plan internals derived)."""
    return PlanIntent(
        tasks=tasks,
        questions=questions,
        hypotheses=hypotheses,
        literature=literature,
    ).model_dump_json()


def _cli_plan_out_json() -> str:
    """Plan cassette for StaticToolRouter — tools_hint are exact ``static:<tool>``."""
    return _intent_json(
        (
            TaskIntent(
                question_indices=(1,),
                goal="overview",
                route="experiment",
                key="EXP-0",
                tools_hint=("static:dataset_overview",),
            ),
            TaskIntent(
                question_indices=(1,),
                goal="cluster",
                route="experiment",
                key="EXP-1",
                tools_hint=("static:chemical_space_clustering",),
            ),
        )
    )


def _adapter(client: LlmClient, router: StaticToolRouter) -> LlmAgentAdapter:
    return LlmAgentAdapter(
        client,
        role_models={
            "planner": _RESEARCH_MODEL,
            "executor-route": _RESEARCH_MODEL,
            "tz_spec": _RESEARCH_MODEL,
        },
        tool_router=router,
        prompts=role_prompt_registry(),
        role_prompts={
            "planner": ROLE_PLANNER_REF,
            "executor-route": ROLE_EXECUTOR_REF,
            "tz_spec": ROLE_TZ_SPEC_REF,
        },
        clock=FakeClock(),
        hitl_policy=HitlPolicy(mode="headless"),
    )


async def _noop_sleep(_delay: float) -> None:
    return None


async def _noop_wait_for(awaitable: Awaitable[_T], *, timeout_s: float) -> _T:
    del timeout_s
    return await awaitable


def _client(bodies: list[bytes], *, monkeypatch: pytest.MonkeyPatch) -> LlmClient:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    return LlmClient(
        _config(max_retries=0),
        CassetteTransport(bodies),
        clock=LlmFakeClock(),
        sleep=_noop_sleep,
        wait_for=_noop_wait_for,
        rng=random.Random(0),
    )


def _cassette_tool(result: dict[str, object] | None = None) -> dict[str, object]:
    return {"result": dict(result or {"ok": True}), "args": {}}


class _TickingFakeClock(FakeClock):
    """FakeClock that advances 0.2s per ``now()`` read.

    The core retry backoff (``RETRY_BACKOFF_S``) is 0.05s, so due retries fire
    on the very next tick without wall-clock sleeps; task/plan deadlines are
    600s+/3600s away, far outside the per-run advance.
    """

    _STEP_S = 0.2

    def now(self) -> datetime:
        self.advance(timedelta(seconds=self._STEP_S))
        return super().now()


def _default_tools() -> dict[str, dict[str, object]]:
    return {
        "dataset_overview": _cassette_tool(),
        "chemical_space_clustering": _cassette_tool(),
    }


async def _read_events(data_root: Path, study_id: str) -> list[StoredEvent]:
    rt = await _open_runtime_helper(data_root, study_id, owner="cli-test-reader", acquire=False)
    try:
        events = await rt.store.read_all()
    finally:
        await _close_runtime_helper(rt)
    return [ev for ev in events if isinstance(ev, StoredEvent)]


def _payloads(events: list[StoredEvent], type_: str) -> list[dict[str, object]]:
    rows = [
        {str(k): v for k, v in ev.payload.items()} for ev in events if ev.type == type_ and isinstance(ev.payload, dict)
    ]
    assert rows, f"no {type_!r} event in journal"
    return rows


async def _run(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[bytes],
    require_hypotheses: bool = False,
    confirmations_needed: int = 1,
    refutations_needed: int = 1,
    refutation_priority: bool = False,
    supported_needed: int = 2,
    max_expansions: int = 2,
    router: StaticToolRouter | None = None,
    max_ticks: int = 20,
    deadline_s: float = 3600.0,
    study_id: str = "two_tasks_study",
    catalog_version: int = CATALOG_VERSION,
    finalize_reserve_s: int = 30,
    lease_ttl_s: float = 60.0,
    max_llm_calls: int = 100,
    max_tokens: int = 1_000_000,
    max_tool_calls: int = 50,
    task_text: str = "Build dataset overview and chemical space clustering.",
    coder_backend: object = None,
    coder_jobs: object = None,
    study_clock: FakeClock | None = None,
) -> tuple[int, dict[str, object], StaticToolRouter]:
    task = tmp_path / "two_tasks.txt"
    task.write_text(task_text, encoding="utf-8")
    if router is None:
        r = StaticToolRouter(tools=_default_tools())
    else:
        r = router
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), r)
    out = io.StringIO()
    code = await run_study(
        task_path=task,
        data_root=tmp_path / "data",
        deadline_s=deadline_s,
        catalog_version=catalog_version,
        catalog=minimal_test_catalog(),
        loaded_profile=_RESEARCH_PROFILE,
        owner="cli-test",
        adapter=adapter,
        out=out,
        study_id=study_id,
        require_hypotheses=require_hypotheses,
        confirmations_needed=confirmations_needed,
        refutations_needed=refutations_needed,
        refutation_priority=refutation_priority,
        supported_needed=supported_needed,
        max_expansions=max_expansions,
        max_attempts=3,
        finalize_reserve_s=finalize_reserve_s,
        lease_ttl_s=lease_ttl_s,
        max_llm_calls=max_llm_calls,
        max_tokens=max_tokens,
        max_tool_calls=max_tool_calls,
        max_ticks=max_ticks,
        tools=r,
        hitl_policy=HitlPolicy(mode="headless"),
        study_clock=study_clock if study_clock is not None else FakeClock(),
        coder_backend=coder_backend,
        coder_jobs=coder_jobs,
    )
    raw = out.getvalue().strip()
    if not raw:
        return code, {}, r
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise AssertionError(f"run_study summary must be a JSON object, got {type(loaded).__name__}")
    summary: dict[str, object] = {str(k): v for k, v in loaded.items()}
    return code, summary, r


@pytest.mark.asyncio
async def test_run_study_fulfilled_records_executor_and_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(
            content=_exec_out_json(tool="dataset_overview"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
    ]
    code, summary, router = await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert code == 0
    assert summary["phase"] == "closed"
    assert summary["verdict"] == "fulfilled"
    assert summary["usage"][0]["role"] == "tz_spec"
    assert any(u["role"] == "planner" for u in summary["usage"])
    assert any(u["role"] == "executor-route" for u in summary["usage"])
    # Observed quality block (bench harness reads it): the two EXP tasks closed
    # SUCCESS with one recorded result each (the SPEC stage result is not
    # experiment evidence), tallies sorted by outcome name.
    assert summary["quality"] == {
        "exp_tasks": 2,
        "evidence": [{"outcome": "SUCCESS", "count": 2}],
        "record_result": 2,
    }
    assert isinstance(router, StaticToolRouter)
    assert router.tool_names == frozenset({"static:dataset_overview", "static:chemical_space_clustering"})


@pytest.mark.asyncio
async def test_require_hypotheses_true_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(CliRefuse, match="hypothesizer") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=[], require_hypotheses=True)
    assert ei.value.code == 5


@pytest.mark.asyncio
async def test_planner_hypotheses_needed_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _intent_json(
        (
            TaskIntent(
                question_indices=(1,),
                goal="g",
                route="experiment",
                key="EXP-0",
                tools_hint=("static:dataset_overview",),
            ),
        ),
        hypotheses=Need(kind="need", needed=True, reason="want hyp"),
        literature=Need(kind="need", needed=False, reason="off"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
    ]
    with pytest.raises(CliRefuse, match="hypothesizer") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert ei.value.code == 5


@pytest.mark.asyncio
async def test_empty_task_file_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(CliRefuse, match="non-empty") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=[],
            task_text="   ",
        )
    assert ei.value.code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("deadline_s", 0.0, "deadline_s must be > 0"),
        ("study_id", "  ", "study_id must be a non-empty str"),
        ("finalize_reserve_s", -1, "finalize_reserve_s must be int >= 0"),
        ("lease_ttl_s", 0.0, "lease_ttl_s must be > 0"),
        ("max_ticks", 0, "max_ticks must be int >= 1"),
        ("confirmations_needed", 0, "confirmations_needed must be int >= 1"),
        ("refutations_needed", 0, "refutations_needed must be int >= 1"),
        ("supported_needed", 1, "supported_needed must be int >= 2"),
        ("max_expansions", -1, "max_expansions must be int >= 0"),
    ],
    ids=[
        "deadline_s",
        "study_id",
        "finalize_reserve_s",
        "lease_ttl_s",
        "max_ticks",
        "confirmations_needed",
        "refutations_needed",
        "supported_needed",
        "max_expansions",
    ],
)
async def test_run_study_arg_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: float | int | str,
    match: str,
) -> None:
    with pytest.raises(CliRefuse, match=match) as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=[], **{field: value})
    assert ei.value.code == 2


@pytest.mark.asyncio
async def test_catalog_version_non_int_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = tmp_path / "t.txt"
    task.write_text("x", encoding="utf-8")
    router = StaticToolRouter(tools={"dataset_overview": _cassette_tool()})
    adapter = _adapter(_client([], monkeypatch=monkeypatch), router)
    extras: dict[str, object] = {"catalog_version": "1"}
    with pytest.raises(CliRefuse, match="catalog_version must be int") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=10.0,
            catalog=minimal_test_catalog(),
            loaded_profile=_RESEARCH_PROFILE,
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="s",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            finalize_reserve_s=1,
            lease_ttl_s=30.0,
            max_llm_calls=10,
            max_tokens=1000,
            max_tool_calls=5,
            max_ticks=5,
            tools=router,
            hitl_policy=HitlPolicy(mode="headless"),
            study_clock=FakeClock(),
            **extras,  # catalog_version deliberately wrong type
        )
    assert ei.value.code == 2


@pytest.mark.asyncio
async def test_planner_literature_needed_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _intent_json(
        (
            TaskIntent(
                question_indices=(1,),
                goal="g",
                route="experiment",
                key="EXP-0",
                tools_hint=("static:dataset_overview",),
            ),
        ),
        hypotheses=Need(kind="need", needed=False, reason="off"),
        literature=Need(kind="need", needed=True, reason="want lit"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
    ]
    with pytest.raises(CliRefuse, match="researcher") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert ei.value.code == 5


@pytest.mark.asyncio
async def test_accept_plan_invalid_dag_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _intent_json(
        (
            TaskIntent(
                question_indices=(1,),
                goal="g",
                route="experiment",
                key="EXP-0",
                tools_hint=("static:dataset_overview",),
                depends_on=("EXP-1",),
            ),
            TaskIntent(
                question_indices=(1,),
                goal="g",
                route="experiment",
                key="EXP-1",
                tools_hint=("static:chemical_space_clustering",),
                depends_on=("EXP-0",),
            ),
        ),
        hypotheses=Need(kind="need", needed=False, reason="off"),
        literature=Need(kind="need", needed=False, reason="off"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
        # ONE bounded replan round: fed the refusal detail, still cyclic → refuse.
        _ok_body(content=plan),
    ]
    with pytest.raises(CliRefuse, match="accept_plan refused.*after one replan round") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert ei.value.code == 3


@pytest.mark.asyncio
async def test_executor_route_bad_schema_retries_then_refuses_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent executor schema violations exhaust the CORE retry budget.

    Each attempt burns the adapter's one repair round (2 bodies) and then
    fails retryable via ``fail_attempt``; ``_fire_due_retries`` re-arms up to
    max_attempts=3. Exhaustion → TaskDone(FAILED) → finalize → non-fulfilled
    verdict → typed refuse rc=7 (exit-0-only-on-fulfilled unchanged).
    """
    single_task_plan = _intent_json(
        (
            TaskIntent(
                question_indices=(1,),
                goal="overview",
                route="experiment",
                key="EXP-0",
                tools_hint=("static:dataset_overview",),
            ),
        )
    )
    bad = [_ok_body(content="not-json{{{", prompt_tokens=1, completion_tokens=1)] * 6
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=single_task_plan, prompt_tokens=10, completion_tokens=20),
        *bad,
    ]
    with pytest.raises(CliRefuse, match="verdict=") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            study_clock=_TickingFakeClock(),
        )
    assert ei.value.code == 7
    events = await _read_events(tmp_path / "data", "two_tasks_study")
    starts = [row for row in _payloads(events, "AttemptStarted") if str(row.get("task_id", "")).endswith("/EXP-0")]
    assert len(starts) == 3, f"want exactly max_attempts=3 EXP-0 attempts, got {len(starts)}"


@pytest.mark.asyncio
async def test_transient_executor_failure_recovers_via_core_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First EXP-0 attempt fails transiently → core retry closes the study fulfilled.

    Attempt 1: schema-invalid executor output (adapter repair also invalid) →
    ``fail_attempt(retryable=True)``; the tick's ``_fire_due_retries`` starts
    attempt 2 under Study.max_attempts; attempt 2 succeeds → fulfilled, rc=0.
    Dispatch order within one loop pass is plan order: EXP-0 attempt 1
    (bodies 3-4), EXP-1 attempt 1 (body 5), then EXP-0's core-fired retry
    attempt 2 (body 6).
    """
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        # EXP-0 attempt 1: invalid output + failed adapter repair round.
        _ok_body(content="not-json{{{", prompt_tokens=1, completion_tokens=1),
        _ok_body(content="still-not-json{{{", prompt_tokens=1, completion_tokens=1),
        # EXP-1 attempt 1: valid.
        _ok_body(content=_exec_out_json(tool="chemical_space_clustering"), prompt_tokens=4, completion_tokens=2),
        # EXP-0 attempt 2 (core-fired retry): valid.
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
    ]
    code, summary, _router = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        study_clock=_TickingFakeClock(),
    )
    assert code == 0
    assert summary["phase"] == "closed"
    assert summary["verdict"] == "fulfilled"
    usage = summary["usage"]
    retry_rows = [u for u in usage if isinstance(u, dict) and "retryable_error" in u]
    assert retry_rows, f"no retryable failure row in usage: {usage!r}"
    assert retry_rows[0]["task_key"] == "EXP-0"
    events = await _read_events(tmp_path / "data", "two_tasks_study")
    starts = [row for row in _payloads(events, "AttemptStarted") if str(row.get("task_id", "")).endswith("/EXP-0")]
    assert len(starts) >= 2, f"core retry did not re-arm EXP-0 (attempts={len(starts)})"
    closes = [
        row
        for row in _payloads(events, "AttemptClosed")
        if row.get("retryable") is True and str(row.get("task_id", "")).endswith("/EXP-0")
    ]
    assert closes, "no retryable AttemptClosed journaled for EXP-0"


@pytest.mark.asyncio
async def test_accept_plan_refusal_replan_round_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First plan refused (cyclic DAG) → ONE replan round fed the refusal detail → valid plan accepted.

    Bounded: exactly two planner calls; round 2's request payload carries
    ``plan_refusal_hint`` (adapter repair pattern at plan level).
    """
    cyclic = _intent_json(
        (
            TaskIntent(
                question_indices=(1,),
                goal="g",
                route="experiment",
                key="EXP-0",
                tools_hint=("static:dataset_overview",),
                depends_on=("EXP-1",),
            ),
            TaskIntent(
                question_indices=(1,),
                goal="g",
                route="experiment",
                key="EXP-1",
                tools_hint=("static:chemical_space_clustering",),
                depends_on=("EXP-0",),
            ),
        )
    )
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=cyclic, prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
        _ok_body(content=_exec_out_json(tool="chemical_space_clustering"), prompt_tokens=4, completion_tokens=2),
    ]
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    transport = CassetteTransport(bodies)
    client = LlmClient(
        _config(max_retries=0),
        transport,
        clock=LlmFakeClock(),
        sleep=_noop_sleep,
        wait_for=_noop_wait_for,
        rng=random.Random(0),
    )
    task = tmp_path / "two_tasks.txt"
    task.write_text("Build dataset overview and chemical space clustering.", encoding="utf-8")
    router = StaticToolRouter(tools=_default_tools())
    out = io.StringIO()
    code = await run_study(
        task_path=task,
        data_root=tmp_path / "data",
        deadline_s=3600.0,
        catalog_version=CATALOG_VERSION,
        catalog=minimal_test_catalog(),
        loaded_profile=_RESEARCH_PROFILE,
        owner="cli-test",
        adapter=_adapter(client, router),
        out=out,
        study_id="replan_study",
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        finalize_reserve_s=30,
        lease_ttl_s=60.0,
        max_llm_calls=100,
        max_tokens=1_000_000,
        max_tool_calls=50,
        max_ticks=20,
        tools=router,
        hitl_policy=HitlPolicy(mode="headless"),
        study_clock=FakeClock(),
    )
    assert code == 0
    summary = json.loads(out.getvalue().strip())
    assert summary["verdict"] == "fulfilled"
    assert summary["study_id"] == "replan_study-r1"
    planner_rows = [u for u in summary["usage"] if u["role"] == "planner"]
    assert len(planner_rows) == 2, f"want exactly 2 planner rounds, got {len(planner_rows)}"

    def _ctx_payload(request: dict[str, object]) -> dict[str, object]:
        messages = request["messages"]
        assert isinstance(messages, list)
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                loaded = json.loads(msg["content"])
                assert isinstance(loaded, dict)
                return loaded
        raise AssertionError("no user message in captured request")

    # LLM calls: tz_spec (once — the replan study reuses the recorded SPEC),
    # planner round 1, planner round 2 (replan), executor ×2.
    assert len(transport.requests) == 5
    round1_ctx = _ctx_payload(transport.requests[1])
    round2_ctx = _ctx_payload(transport.requests[2])
    assert round1_ctx.get("plan_refusal_hint") == ""
    assert "accept_plan refused" in round2_ctx["plan_refusal_hint"]
    assert round2_ctx["study_id"] == "replan_study-r1"


@pytest.mark.asyncio
async def test_derive_refusal_replans_in_place_same_study(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First planner round derives duplicate task keys → PlanDeriveError.

    The ONE replan round runs IN PLACE on the SAME runtime (derive refusals
    do not finalize the study): no reopen, no ``-r1`` journal, tz_spec not
    re-called, round-2 payload carries the derive refusal hint, and the
    corrected plan closes fulfilled under the ORIGINAL study id.
    """
    dup_keys = _intent_json(
        (
            TaskIntent(
                question_indices=(1,),
                goal="overview",
                route="experiment",
                key="EXP-0",
                tools_hint=("static:dataset_overview",),
            ),
            TaskIntent(
                question_indices=(1,),
                goal="overview again",
                route="experiment",
                key="EXP-0",
                tools_hint=("static:dataset_overview",),
            ),
        )
    )
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        # Round 1: schema-valid intent, derive refuses (duplicate task keys).
        _ok_body(content=dup_keys, prompt_tokens=10, completion_tokens=20),
        # Round 2 (same study): valid plan.
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
        _ok_body(content=_exec_out_json(tool="chemical_space_clustering"), prompt_tokens=4, completion_tokens=2),
    ]
    code, summary, _router = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        study_id="derive_retry_study",
    )
    assert code == 0
    assert summary["verdict"] == "fulfilled"
    # Same-study contract: the ORIGINAL study id closes and no -r1 journal exists.
    assert summary["study_id"] == "derive_retry_study"
    assert not (tmp_path / "data" / "derive_retry_study-r1").exists()
    planner_rows = [u for u in summary["usage"] if u["role"] == "planner"]
    assert len(planner_rows) == 2
    spec_rows = [u for u in summary["usage"] if u["role"] == "tz_spec"]
    assert len(spec_rows) == 1, "in-place replan must not re-call tz_spec"
    events = await _read_events(tmp_path / "data", "derive_retry_study")
    assert _payloads(events, "PlanAccepted"), "same-study replan must accept on the original journal"


@pytest.mark.asyncio
async def test_executor_drift_refusals_are_terminal_not_retried() -> None:
    """Runtime-drift refusals (no executor / attempt row / task row) are
    terminal rc=4 BEFORE any adapter call — infra drift never burns the core
    retry budget (no fail_attempt, no retryable_error usage row)."""

    class _Rt:
        def __init__(self, *, executor: object, attempts: dict[str, object], tasks: dict[str, object]) -> None:
            self.executor = executor
            self.attempts = attempts
            self.tasks = tasks

        async def fail_attempt(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("drift refusal must not be retried via fail_attempt")

    class _Adapter:
        async def run(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("no adapter call on a drift refusal")

    budget = Budget(
        max_llm_calls=1,
        max_tokens=100,
        max_cost_rub=None,
        max_tool_calls=1,
        deadline=datetime(2027, 1, 1, tzinfo=UTC),
    )
    draft = TaskSpecDraft(
        task_key="EXP-0",
        title="t",
        goal="g",
        route="experiment",
        depends_on=(),
        addresses=(),
        tools_hint=(),
        expected_outputs=(),
        criteria=(),
    )
    usages: list[dict[str, object]] = []
    cases = [
        (_Rt(executor=None, attempts={"a1": {"task_id": "t1"}}, tasks={"t1": object()}), "no executor"),
        (_Rt(executor=object(), attempts={}, tasks={"t1": object()}), "missing from runtime.attempts"),
        (_Rt(executor=object(), attempts={"a1": {"task_id": "tX"}}, tasks={"t1": object()}), "no resolvable task"),
    ]
    for rt_stub, match in cases:
        with pytest.raises(CliRefuse, match=match) as ei:
            await _execute_executor_attempt(
                rt_stub,
                adapter=_Adapter(),
                draft=draft,
                aid="a1",
                planned=(),
                study_id="drift_study",
                registered_inputs=(),
                allow_tools=(),
                role_budget=budget,
                usages=usages,
            )
        assert ei.value.code == 4
    assert usages == [], "drift refusals must not journal retryable failures"


@pytest.mark.asyncio
async def test_max_ticks_exhausted_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coscientist.api.runtime import StudyRuntime

    async def _noop_tick(self: StudyRuntime) -> None:
        del self
        return None

    monkeypatch.setattr(StudyRuntime, "tick", _noop_tick)
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_cli_plan_out_json()),
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
        _ok_body(content=_exec_out_json(tool="chemical_space_clustering")),
    ]
    with pytest.raises(CliRefuse, match="max_ticks=") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies, max_ticks=1)
    assert ei.value.code == 6


@pytest.mark.asyncio
async def test_retry_loop_is_bounded_by_tick_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scheduled-but-never-due retry (frozen clock) cannot spin forever.

    The failed attempt arms the core retry; with a frozen study clock the
    backoff never elapses, so the recovery loop iterates to the max_ticks cap
    and refuses typed (rc=6) — the CLI loop itself is bounded independently
    of the core's attempt/deadline bounds.
    """
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(
            content=_intent_json(
                (
                    TaskIntent(
                        question_indices=(1,),
                        goal="overview",
                        route="experiment",
                        key="EXP-0",
                        tools_hint=("static:dataset_overview",),
                    ),
                )
            ),
            prompt_tokens=10,
            completion_tokens=20,
        ),
        _ok_body(content="not-json{{{", prompt_tokens=1, completion_tokens=1),
        _ok_body(content="still-not-json{{{", prompt_tokens=1, completion_tokens=1),
    ]
    with pytest.raises(CliRefuse, match="max_ticks=") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies, max_ticks=5)
    assert ei.value.code == 6


@pytest.mark.asyncio
async def test_tools_hint_outside_allow_list_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema-checked: the planner request enum (ctx.tool_ids) refuses unknown ids
    before the run.py allow-list backstop can ever see them."""

    plan = _intent_json(
        (TaskIntent(question_indices=(1,), goal="g", route="experiment", key="EXP-0", tools_hint=("unknown_tool",)),),
        hypotheses=Need(kind="need", needed=False, reason="off"),
        literature=Need(kind="need", needed=False, reason="off"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
        # repair retry round: same body, still outside the enum
        _ok_body(content=plan),
    ]
    with pytest.raises(CliRefuse, match="agent error") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies)
    assert ei.value.code == 4


def test_main_missing_required_args_exits_nonzero() -> None:
    with pytest.raises(SystemExit, match="2") as ei:
        main(["run", "missing_task.txt"])
    assert ei.value.code == 2


def _main_argv(tmp_path: Path, *, hitl_mode: str | None) -> list[str]:
    """Minimal valid ``coscientist run`` argv; omit ``--hitl-mode`` when None."""
    task = tmp_path / "task.txt"
    task.write_text("study request", encoding="utf-8")
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(minimal_test_catalog().model_dump_json(), encoding="utf-8")
    llm_path = tmp_path / "llm.json"
    llm_path.write_text(_config().model_dump_json(), encoding="utf-8")
    tools_path = tmp_path / "tools.json"
    tools_path.write_text(json.dumps(_default_tools()), encoding="utf-8")
    argv = [
        "run",
        str(task),
        "--data-root",
        str(tmp_path / "data"),
        "--deadline-s",
        "60",
        "--catalog-version",
        str(CATALOG_VERSION),
        "--catalog",
        str(catalog_path),
        "--profile",
        "research",
        "--tool-catalog-version",
        "2",
        "--owner",
        "cli-test",
        "--llm-config",
        str(llm_path),
        "--planner-model",
        _RESEARCH_MODEL,
        "--executor-model",
        _RESEARCH_MODEL,
        "--tz-spec-model",
        _RESEARCH_MODEL,
        "--tools-json",
        str(tools_path),
        "--study-id",
        "hitl_mode_study",
        "--require-hypotheses",
        "false",
        "--confirmations-needed",
        "1",
        "--refutations-needed",
        "1",
        "--refutation-priority",
        "false",
        "--supported-needed",
        "2",
        "--max-expansions",
        "2",
        "--finalize-reserve-s",
        "30",
        "--lease-ttl-s",
        "60",
        "--rng-seed",
        "0",
        "--max-llm-calls",
        "10",
        "--max-tokens",
        "1000",
        "--max-tool-calls",
        "10",
        "--max-ticks",
        "5",
    ]
    if hitl_mode is not None:
        argv.extend(["--hitl-mode", hitl_mode])
    return argv


def test_main_missing_hitl_mode_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    with pytest.raises(SystemExit, match="2") as ei:
        main(_main_argv(tmp_path, hitl_mode=None))
    assert ei.value.code == 2


@pytest.mark.parametrize(
    "mode",
    ["interactive", "auto", "headless"],
    ids=["interactive", "auto", "headless"],
)
def test_main_hitl_mode_wires_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    seen: dict[str, object] = {}

    async def _capture_run_study(**kwargs: object) -> int:
        seen["hitl_policy"] = kwargs["hitl_policy"]
        return 0

    monkeypatch.setattr("coscientist.cli.run.run_study", _capture_run_study)
    monkeypatch.setattr(
        "coscientist.cli.run.load_catalog",
        lambda *args, **kwargs: minimal_test_catalog(),
    )
    code = main(_main_argv(tmp_path, hitl_mode=mode))
    assert code == 0
    policy = seen["hitl_policy"]
    assert isinstance(policy, HitlPolicy)
    assert policy.mode == mode


def test_parse_bool_direct() -> None:
    from coscientist.cli.run import _parse_bool

    assert _parse_bool("true") is True
    assert _parse_bool("false") is False
    with pytest.raises(argparse.ArgumentTypeError, match="true\\|false"):
        _parse_bool("yes")


def test_cli_refuse_rejects_zero_code() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        CliRefuse("x", code=0)


def test_static_tool_router_resolve_and_refuse_args() -> None:
    router = StaticToolRouter(tools={"t1": _cassette_tool()})
    assert router.tool_names == frozenset({"static:t1"})
    found = router.resolve(ToolNeed(kind="by_name", value="static:t1"))
    assert found.status == "tool_found"
    assert found.server_name == "static"
    assert found.tool_name == "t1"
    assert router.resolve(ToolNeed(kind="by_name", value="t1")).status == "tool_missing"
    assert router.resolve(ToolNeed(kind="by_name", value="missing")).status == "tool_missing"


@pytest.mark.asyncio
async def test_static_tool_router_call_empty_args_ok() -> None:
    from coscientist.core.execution.domain import HitlPolicy
    from coscientist.tools.contract import CallCtx

    router = StaticToolRouter(tools={"t1": _cassette_tool()})
    ctx = CallCtx(study_id="s", task_key="k", attempt_id="a", hitl_policy=HitlPolicy(mode="headless"))
    result = await router.call("static", "t1", {}, ctx)
    assert result.data == {"ok": True}


@pytest.mark.asyncio
async def test_static_tool_router_call_nonempty_args_refuses() -> None:
    from coscientist.core.execution.domain import HitlPolicy
    from coscientist.tools.contract import CallCtx

    router = StaticToolRouter(tools={"t1": _cassette_tool()})
    ctx = CallCtx(study_id="s", task_key="k", attempt_id="a", hitl_policy=HitlPolicy(mode="headless"))
    with pytest.raises(ValueError, match="args mismatch"):
        await router.call("static", "t1", {"x": 1}, ctx)


@pytest.mark.asyncio
async def test_http_transport_timeout_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class _BoomClient:
        async def __aenter__(self) -> _BoomClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _BoomClient())
    transport = HttpxTransport()
    with pytest.raises(LlmTransportError, match="timeout") as ei:
        await transport.request("POST", "https://example.test", headers={}, body=b"{}", timeout_s=1.0)
    assert ei.value.retryable is True


@pytest.mark.asyncio
async def test_http_transport_invalid_url_not_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class _BoomClient:
        async def __aenter__(self) -> _BoomClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise httpx.InvalidURL("bad")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _BoomClient())
    transport = HttpxTransport()
    with pytest.raises(LlmTransportError, match="InvalidURL") as ei:
        await transport.request("POST", "not-a-url", headers={}, body=b"{}", timeout_s=1.0)
    assert ei.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "retryable", "match"),
    [
        ("connect", True, "ConnectError"),
        ("remote_protocol", True, "RemoteProtocolError"),
        ("http_status", False, "HTTPStatusError"),
    ],
    ids=["network", "remote_protocol", "http_error"],
)
async def test_http_transport_error_retryable_branches(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    retryable: bool,
    match: str,
) -> None:
    import httpx

    class _BoomClient:
        async def __aenter__(self) -> _BoomClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            if kind == "http_status":
                request = httpx.Request("POST", "https://example.test")
                response = httpx.Response(500, request=request)
                raise httpx.HTTPStatusError("boom", request=request, response=response)
            if kind == "remote_protocol":
                raise httpx.RemoteProtocolError("boom")
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _BoomClient())
    transport = HttpxTransport()
    with pytest.raises(LlmTransportError, match=match) as ei:
        await transport.request("POST", "https://example.test", headers={}, body=b"{}", timeout_s=1.0)
    assert ei.value.retryable is retryable


def test_wall_llm_clock_methods() -> None:
    clock = WallLlmClock()
    assert clock.monotonic() >= 0.0
    assert clock.wall_utc().tzinfo is not None


@pytest.mark.asyncio
async def test_wall_sleep_and_wait_for() -> None:
    await wall_sleep(0.0)
    result = await wall_wait_for(asyncio_sleep_zero(), timeout_s=1.0)
    assert result == 1


async def asyncio_sleep_zero() -> int:
    return 1


@pytest.mark.asyncio
async def test_wired_hypothesizer_without_role_model_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adapter instance without the wired role's model entry → typed refuse (rc=4).

    The output SCHEMA exists (supports_role), but a caller-built adapter without
    the profile-backed role_models entry refuses at run — no invented model.
    Post-merge precedence: the instance-level role_models/role_prompts check
    (main's critic/reviewer wiring) fires before the schema-model ``.get()``
    guard; both are typed AgentError → rc=4 (fail-closed either way).
    """
    plan = _intent_json(
        (
            TaskIntent(
                question_indices=(1,),
                goal="g",
                route="experiment",
                key="EXP-0",
                tools_hint=("static:dataset_overview",),
            ),
        ),
        hypotheses=Need(kind="need", needed=True, reason="want hyp"),
        literature=Need(kind="need", needed=False, reason="off"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
    ]
    task = tmp_path / "t.txt"
    task.write_text("x", encoding="utf-8")
    router = StaticToolRouter(tools=_default_tools())
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), router)
    with pytest.raises(CliRefuse, match="not configured on this adapter") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=3600.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            loaded_profile=_RESEARCH_PROFILE,
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="wired_hyp",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            finalize_reserve_s=30,
            lease_ttl_s=60.0,
            max_llm_calls=100,
            max_tokens=1_000_000,
            max_tool_calls=50,
            max_ticks=20,
            tools=router,
            hitl_policy=HitlPolicy(mode="headless"),
            wired_roles=frozenset({"hypothesizer"}),
            study_clock=FakeClock(),
        )
    assert ei.value.code == 4


@pytest.mark.asyncio
async def test_wired_report_without_llm_client_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--wired-roles report without a passed LlmClient → typed refuse (code 5)."""
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
    ]
    task = tmp_path / "t.txt"
    task.write_text("Build dataset overview and chemical space clustering.", encoding="utf-8")
    router = StaticToolRouter(tools=_default_tools())
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), router)
    with pytest.raises(CliRefuse, match="report declared in --wired-roles") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=3600.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            loaded_profile=_RESEARCH_PROFILE,
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="wired_report",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            finalize_reserve_s=30,
            lease_ttl_s=60.0,
            max_llm_calls=100,
            max_tokens=1_000_000,
            max_tool_calls=50,
            max_ticks=20,
            tools=router,
            hitl_policy=HitlPolicy(mode="headless"),
            wired_roles=frozenset({"report"}),
            study_clock=FakeClock(),
        )
    assert ei.value.code == 5


def test_parse_wired_roles() -> None:
    from coscientist.cli.run import _parse_wired_roles

    assert _parse_wired_roles("[]") == frozenset()
    assert _parse_wired_roles('["hypothesizer","report"]') == frozenset({"hypothesizer", "report"})
    assert _parse_wired_roles('["critic","reviewer"]') == frozenset({"critic", "reviewer"})
    with pytest.raises(CliRefuse, match="unknown optional role") as ei:
        _parse_wired_roles('["teleporter"]')
    assert ei.value.code == 2
    with pytest.raises(CliRefuse, match="JSON array") as ei2:
        _parse_wired_roles("hypothesizer")
    assert ei2.value.code == 2


def test_main_tools_json_and_mcp_bundle_mutex(tmp_path: Path) -> None:
    argv = _main_argv(tmp_path, hitl_mode="headless")
    argv.extend(["--mcp-tool-bundle", str(tmp_path / "bundle.json")])
    with pytest.raises(SystemExit, match="2") as ei:
        main(argv)
    assert ei.value.code == 2


def test_main_mcp_bundle_requires_tool_call_args(tmp_path: Path) -> None:
    argv = _main_argv(tmp_path, hitl_mode="headless")
    # Replace --tools-json with --mcp-tool-bundle without --tool-call-args.
    tools_idx = argv.index("--tools-json")
    bundle = tmp_path / "bundle.json"
    bundle.write_text("{}", encoding="utf-8")
    argv[tools_idx] = "--mcp-tool-bundle"
    argv[tools_idx + 1] = str(bundle)
    with pytest.raises(SystemExit) as ei:
        main(argv)
    assert ei.value.code == 2


class _ArgsGapSurface:
    """Allow-list advertises a name ``planned_calls`` cannot satisfy (pre-fix Live bug)."""

    def __init__(self, base: StaticToolRouter, *, gap: str) -> None:
        self._base = base
        self._gap = gap

    @property
    def tool_names(self) -> frozenset[str]:
        return self._base.tool_names | frozenset({self._gap})

    def named_tools(self) -> list[object]:
        from coscientist.tools.contract import ToolSpec

        class _Named:
            def __init__(self, name: str) -> None:
                self.spec = ToolSpec(name=name)

        return list(self._base.named_tools()) + [_Named(self._gap)]

    def planned_calls(self, hints: object) -> tuple[object, ...]:
        return self._base.planned_calls(hints)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_planned_calls_keyerror_refuses_before_start_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tools_hint in allow-list but missing call args → CliRefuse, no EXP AttemptStarted."""
    from coscientist.api.runtime import StudyRuntime

    gap = "static:ghost"
    plan = _intent_json(
        (TaskIntent(question_indices=(1,), goal="g", route="experiment", key="EXP-0", tools_hint=(gap,)),),
        hypotheses=Need(kind="need", needed=False, reason="off"),
        literature=Need(kind="need", needed=False, reason="off"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
    ]
    started: list[str] = []
    real_start = StudyRuntime.start_attempt

    async def _track_start(self: StudyRuntime, task_key_or_id: str, **kwargs: object) -> str | None:
        aid = await real_start(self, task_key_or_id, **kwargs)  # type: ignore[arg-type]
        started.append(str(task_key_or_id))
        return aid

    monkeypatch.setattr(StudyRuntime, "start_attempt", _track_start)
    task = tmp_path / "t.txt"
    task.write_text("x", encoding="utf-8")
    router = StaticToolRouter(tools=_default_tools())
    surface = _ArgsGapSurface(router, gap=gap)
    adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), router)
    with pytest.raises(CliRefuse, match="missing declared call args") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=3600.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            loaded_profile=_RESEARCH_PROFILE,
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="args_gap",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            finalize_reserve_s=30,
            lease_ttl_s=60.0,
            max_llm_calls=100,
            max_tokens=1_000_000,
            max_tool_calls=50,
            max_ticks=20,
            tools=surface,  # type: ignore[arg-type]
            hitl_policy=HitlPolicy(mode="headless"),
            study_clock=FakeClock(),
        )
    assert ei.value.code == 3
    assert "EXP-0" not in started


@pytest.mark.asyncio
async def test_wired_report_without_profile_role_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wired report + LlmClient but profile lacks the report role → code 5."""
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_cli_plan_out_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=4, completion_tokens=2),
        _ok_body(
            content=_exec_out_json(tool="chemical_space_clustering"),
            prompt_tokens=4,
            completion_tokens=2,
        ),
    ]
    task = tmp_path / "t.txt"
    task.write_text("Build dataset overview and chemical space clustering.", encoding="utf-8")
    router = StaticToolRouter(tools=_default_tools())
    client = _client(bodies, monkeypatch=monkeypatch)
    adapter = _adapter(client, router)
    stripped = _RESEARCH_PROFILE.model_copy(
        update={"roles": {k: v for k, v in _RESEARCH_PROFILE.roles.items() if k != "report"}}
    )
    with pytest.raises(CliRefuse, match="has no report role") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=3600.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            loaded_profile=stripped,
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="wired_report_no_role",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            finalize_reserve_s=30,
            lease_ttl_s=60.0,
            max_llm_calls=100,
            max_tokens=1_000_000,
            max_tool_calls=50,
            max_ticks=20,
            tools=router,
            hitl_policy=HitlPolicy(mode="headless"),
            wired_roles=frozenset({"report"}),
            study_clock=FakeClock(),
            llm_client=client,
        )
    assert ei.value.code == 5


def test_main_unknown_profile_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    argv = _main_argv(tmp_path, hitl_mode="headless")
    idx = argv.index("--profile")
    argv[idx + 1] = "medicine"
    with pytest.raises(SystemExit) as ei:
        main(argv)
    assert ei.value.code == 2


def test_main_tool_catalog_version_pin_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    argv = _main_argv(tmp_path, hitl_mode="headless")
    idx = argv.index("--tool-catalog-version")
    argv[idx + 1] = "99"
    with pytest.raises(SystemExit) as ei:
        main(argv)
    assert ei.value.code == 2


def test_main_model_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    argv = _main_argv(tmp_path, hitl_mode="headless")
    idx = argv.index("--planner-model")
    argv[idx + 1] = "not-the-profile-model"
    with pytest.raises(SystemExit) as ei:
        main(argv)
    assert ei.value.code == 2


@pytest.mark.asyncio
async def test_run_study_loaded_profile_not_profile_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = tmp_path / "t.txt"
    task.write_text("x", encoding="utf-8")
    router = StaticToolRouter(tools=_default_tools())
    adapter = _adapter(_client([], monkeypatch=monkeypatch), router)
    with pytest.raises(CliRefuse, match="loaded_profile must be Profile") as ei:
        await run_study(
            task_path=task,
            data_root=tmp_path / "data",
            deadline_s=10.0,
            catalog_version=CATALOG_VERSION,
            catalog=minimal_test_catalog(),
            loaded_profile="research",  # type: ignore[arg-type]
            owner="cli-test",
            adapter=adapter,
            out=io.StringIO(),
            study_id="bad_profile",
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            finalize_reserve_s=1,
            lease_ttl_s=30.0,
            max_llm_calls=10,
            max_tokens=1000,
            max_tool_calls=5,
            max_ticks=5,
            tools=router,
            hitl_policy=HitlPolicy(mode="headless"),
            study_clock=FakeClock(),
        )
    assert ei.value.code == 2


def test_assert_profile_tools_on_surface_refuses_unconditionally() -> None:
    """Exact catalog resolve is unconditional now: missing surface tools refuse (code=3)."""
    from coscientist.cli import run as run_mod

    profile = load_profile("research", expected_tool_catalog_version=2)
    roles = dict(profile.roles)
    roles["planner"] = roles["planner"].model_copy(update={"tools": ("planner_search",)})
    profile = profile.model_copy(update={"roles": roles})

    class _EmptySurface:
        tool_names: frozenset[str] = frozenset()

    with pytest.raises(CliRefuse) as exc:
        run_mod._assert_profile_tools_on_surface(profile, _EmptySurface())
    assert exc.value.code == 3
    assert "no blanket-allow" in exc.value.detail


# --- coder dispatch (code-execution delivery via tools_hint "coder") --------


def _coder_job_payload() -> dict[str, object]:
    """One declared echo-style job for task EXP-0 (argv/limits never invented)."""
    return {
        "EXP-0": {
            "argv": ["python3", "-c", "print('ok')"],
            "inputs": {},
            "env": {},
            "outputs": [{"name": "coder", "source": "stdout", "media_type": "text/plain"}],
            "limits": {
                "timeout_s": 30.0,
                "memory_mb": 256,
                "max_output_bytes": 1_000_000,
                "pids_max": 256,
                "rlimit_nproc": 256,
                "allow_network": True,
                "allow_cgroup_degraded": False,
                "reap_timeout_s": 5.0,
                "output_drain_join_s": 5.0,
            },
        }
    }


def _coder_plan_json(*, route: str = "experiment") -> str:
    return _intent_json(
        (
            TaskIntent(
                question_indices=(1,), goal="run the analysis code", route=route, key="EXP-0", tools_hint=("coder",)
            ),
        )
    )


def _coder_router() -> StaticToolRouter:
    router = StaticToolRouter(tools=_default_tools())
    router.attach_coder_route("local-python")
    return router


@pytest.mark.asyncio
async def test_coder_task_local_python_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: hint 'coder' + explicit backend dispatches a real local-python
    job; Job* lifecycle events are journaled and job artifacts are the output."""
    from coscientist.cli.run import _parse_coder_jobs
    from coscientist.core.events.store import SqliteEventStore
    from coscientist.core.execution.artifacts import LocalArtifactStore

    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_coder_plan_json(), prompt_tokens=10, completion_tokens=20),
    ]
    code, summary, _router = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        router=_coder_router(),
        coder_backend="local-python",
        coder_jobs=_parse_coder_jobs(_coder_job_payload()),
        study_id="coder_study",
    )
    assert code == 0
    assert summary["phase"] == "closed"
    assert summary["verdict"] == "fulfilled"
    coder_rows = [u for u in summary["usage"] if u["role"] == "coder"]
    assert len(coder_rows) == 1
    assert coder_rows[0]["status"] == "succeeded"
    artifact_ids = coder_rows[0]["artifact_ids"]
    assert len(artifact_ids) == 1
    # No executor-route LLM call happened for the coder task.
    assert not [u for u in summary["usage"] if u["role"] == "executor-route"]

    store = SqliteEventStore("coder_study", tmp_path / "data" / "coder_study" / "events.db", clock=FakeClock())
    events = await store.read_all()
    types = [e.type for e in events]
    assert "JobSubmitted" in types
    assert "JobStatusChanged" in types
    assert "EffectIntended" in types
    assert "EffectDone" in types
    assert "ToolCalled" in types
    assert "ResultRecorded" in types
    await store.close()

    arts = LocalArtifactStore(tmp_path / "data" / "coder_study" / "arts")
    meta = arts.get_meta(artifact_ids[0])
    assert meta is not None
    assert meta.name == "coder"
    assert meta.producer == "sandbox"
    assert arts.read(artifact_ids[0]) == b"ok\n"


@pytest.mark.asyncio
async def test_coder_task_without_backend_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No explicit --coder-backend ⇒ typed refuse (fail-closed, no stub science)."""
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_coder_plan_json()),
    ]
    with pytest.raises(CliRefuse, match="--coder-backend") as ei:
        await _run(tmp_path, monkeypatch=monkeypatch, bodies=bodies, router=_coder_router())
    assert ei.value.code == 5


@pytest.mark.asyncio
async def test_coder_transfer_dep_outputs_into_job_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task-to-task file transfer: a coder task's job workspace receives the
    recorded outputs of its depends_on tasks as dep{N}_{output} files (bytes
    from the CAS journal fold — never model claims)."""
    from coscientist.cli.run import _parse_coder_jobs

    plan = _intent_json(
        (
            TaskIntent(question_indices=(1,), goal="produce", route="experiment", key="produce", tools_hint=("coder",)),
            TaskIntent(
                question_indices=(1,),
                goal="consume",
                route="experiment",
                key="consume",
                tools_hint=("coder",),
                depends_on=("produce",),
            ),
        )
    )
    jp = _coder_job_payload()["EXP-0"]
    consumer = json.loads(json.dumps(jp))
    consumer["argv"] = [
        "python3",
        "-c",
        "import hashlib,sys; sys.stdout.write(hashlib.sha256(open('dep1_coder','rb').read()).hexdigest())",
    ]
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=plan, prompt_tokens=10, completion_tokens=20),
    ]
    code, summary, _ = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        router=_coder_router(),
        coder_backend="local-python",
        coder_jobs=_parse_coder_jobs(
            {
                "j1": json.loads(json.dumps(jp)),
                "j2": consumer,
            }
        ),
        study_id="transfer_study",
    )
    assert code == 0
    assert summary["verdict"] == "fulfilled"


@pytest.mark.asyncio
async def test_coder_tasks_two_bind_by_plan_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """N:N: equal counts with unknown planner keys bind by plan order —
    jobs declared in the order the scenario describes the tasks."""
    from coscientist.cli.run import _parse_coder_jobs

    plan = _intent_json(
        (
            TaskIntent(question_indices=(1,), goal="copy a", route="experiment", key="copy_a", tools_hint=("coder",)),
            TaskIntent(question_indices=(1,), goal="copy b", route="experiment", key="copy_b", tools_hint=("coder",)),
        )
    )
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=plan, prompt_tokens=10, completion_tokens=20),
    ]
    jp = _coder_job_payload()["EXP-0"]
    result = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        router=_coder_router(),
        coder_backend="local-python",
        coder_jobs=_parse_coder_jobs({"any1": jp, "any2": jp}),
    )
    code, summary, _ = result
    assert code == 0
    assert summary["verdict"] == "fulfilled"


@pytest.mark.asyncio
async def test_coder_task_single_job_binds_by_position(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One coder task + one declared job (any key) binds: planner assigns keys
    the operator cannot know in advance; unambiguous 1:1 mapping binds."""
    from coscientist.cli.run import _parse_coder_jobs

    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_coder_plan_json()),
    ]
    result = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        router=_coder_router(),
        coder_backend="local-python",
        coder_jobs=_parse_coder_jobs({"any-key": _coder_job_payload()["EXP-0"]}),  # type: ignore[index]
    )
    code, summary, _ = result
    assert code == 0
    assert summary["verdict"] == "fulfilled"


async def test_coder_task_without_declared_job_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Backend set, ambiguous --coder-jobs (two wrong keys) ⇒ typed refuse.

    One coder task + one wrong-key job now binds (planner assigns keys the
    operator cannot know); ambiguity still refuses.
    """
    from coscientist.cli.run import _parse_coder_jobs

    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_coder_plan_json()),
    ]
    with pytest.raises(CliRefuse, match="cannot be bound to declared jobs") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            router=_coder_router(),
            coder_backend="local-python",
            coder_jobs=_parse_coder_jobs(
                {
                    "OTHER": _coder_job_payload()["EXP-0"],  # type: ignore[index]
                    "THIRD": _coder_job_payload()["EXP-0"],  # type: ignore[index]
                }
            ),
        )
    assert ei.value.code == 3


@pytest.mark.asyncio
async def test_coder_job_decl_parse_refuses_bad_limits() -> None:
    from coscientist.cli.run import _parse_coder_jobs

    payload = _coder_job_payload()
    payload["EXP-0"]["limits"]["timeout_s"] = 30  # int, not float — strict refuse
    with pytest.raises(CliRefuse, match="limits refused"):
        _parse_coder_jobs(payload)


def test_main_coder_jobs_requires_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    jobs_path = tmp_path / "coder_jobs.json"
    jobs_path.write_text(json.dumps(_coder_job_payload()), encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        main(_main_argv(tmp_path, hitl_mode="headless") + ["--coder-jobs", str(jobs_path)])
    assert ei.value.code == 2


# --- reviewer LOW findings: coder dispatch refusal gaps (L1-L3) --------------


@pytest.mark.asyncio
async def test_coder_task_non_experiment_route_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L1: route='literature' + coder hint → typed refuse (code execution is
    experiment delivery only)."""
    from coscientist.cli.run import _parse_coder_jobs

    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_coder_plan_json(route="literature")),
    ]
    with pytest.raises(CliRefuse, match="non-experiment") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            router=_coder_router(),
            coder_backend="local-python",
            coder_jobs=_parse_coder_jobs(_coder_job_payload()),
        )
    assert ei.value.code == 3


@pytest.mark.asyncio
async def test_coder_task_required_output_not_declared_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L2a: derived required output 'coder' not covered by --coder-jobs outputs
    → typed refuse before any attempt starts."""
    from coscientist.cli.run import _parse_coder_jobs

    payload = _coder_job_payload()
    payload["EXP-0"]["outputs"] = [{"name": "something_else", "source": "stdout", "media_type": "text/plain"}]
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_coder_plan_json()),
    ]
    with pytest.raises(CliRefuse, match="not declared") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            router=_coder_router(),
            coder_backend="local-python",
            coder_jobs=_parse_coder_jobs(payload),
        )
    assert ei.value.code == 3


@pytest.mark.asyncio
async def test_coder_task_failed_job_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L2b: non-succeeded job outcome (exit 3) → typed refuse, no fake SUCCESS."""
    from coscientist.cli.run import _parse_coder_jobs

    payload = _coder_job_payload()
    payload["EXP-0"]["argv"] = ["python3", "-c", "import sys; sys.exit(3)"]
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_coder_plan_json()),
    ]
    with pytest.raises(CliRefuse, match=r"coder job for task 'EXP-0' failed") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            router=_coder_router(),
            coder_backend="local-python",
            coder_jobs=_parse_coder_jobs(payload),
        )
    assert ei.value.code == 4
    assert "exit:3" in ei.value.detail


@pytest.mark.asyncio
async def test_dispatch_coder_task_mixed_hints_refuse() -> None:
    """L3: _dispatch_coder_task itself refuses mixed hints (unreachable via
    derive, which already refuses — the dispatch gate is the backstop)."""
    from datetime import UTC, datetime, timedelta

    from coscientist.agents.schemas import OutputSpecDraft, TaskSpecDraft
    from coscientist.cli.run import _dispatch_coder_task, _parse_coder_jobs
    from coscientist.core.events.clock import FakeClock
    from coscientist.core.execution.domain import HitlPolicy

    draft = TaskSpecDraft(
        task_key="EXP-0",
        title="t",
        goal="g",
        route="experiment",
        depends_on=(),
        addresses=("q1",),
        tools_hint=("coder", "static:dataset_overview"),
        expected_outputs=(
            OutputSpecDraft(
                name="coder",
                media_type="application/json",
                schema_ref=None,
                required=True,
                rubric=None,
            ),
        ),
        criteria=(),
    )
    with pytest.raises(CliRefuse, match="must be the only hint") as ei:
        await _dispatch_coder_task(
            None,  # type: ignore[arg-type]
            draft=draft,
            study_id="s",
            backend="local-python",
            decl=list(_parse_coder_jobs({"x": _coder_job_payload()["EXP-0"]}).values())[0],
            writer_journal=None,  # type: ignore[arg-type]
            clock=FakeClock(),
            hitl_policy=HitlPolicy(mode="headless"),
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            usages=[],
        )
    assert ei.value.code == 3
