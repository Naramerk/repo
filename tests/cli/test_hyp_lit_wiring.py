"""Wired need-stage roles: hypothesizer (HYP) + researcher (LIT).

GAP №8: planner Need flags were parsed, but wired HYP/LIT refused before any
run ("CLI journal record not wired"). These tests pin the honest wired path
(start_attempt → adapter run → journal record → DAG task closes non-SKIPPED),
the fail-closed refuses (unwired / no surface / no profile role / route drift),
and the no-invented-citations gate on the LIT record path.
"""

from __future__ import annotations

import io
import json
import random
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

import pytest

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.critic import CriticIssue, CriticVerdict
from coscientist.agents.prompts.roles import (
    ROLE_CRITIC_REF,
    ROLE_EXECUTOR_REF,
    ROLE_HYPOTHESIZER_REF,
    ROLE_PLANNER_REF,
    ROLE_RESEARCHER_REF,
    ROLE_TZ_SPEC_REF,
    role_prompt_registry,
)
from coscientist.agents.research_spec import Constraint, Goal, ResearchSpec, SuccessMetric
from coscientist.agents.schemas import (
    AgentRun,
    ExecutorRouteOut,
    HypothesisDraft,
    HypothesisOut,
    LiteratureOut,
    PaperRef,
    PlanIntent,
    QuestionIntent,
    TaskIntent,
    ToolCallRef,
)
from coscientist.checks.catalog import CATALOG_VERSION
from coscientist.cli.errors import CliRefuse
from coscientist.cli.journal import record_hypotheses, record_literature
from coscientist.cli.run import run_study
from coscientist.cli.static_router import StaticToolRouter
from coscientist.core.events.clock import FakeClock
from coscientist.core.events.events import StoredEvent
from coscientist.core.execution.domain import HitlPolicy, Need
from coscientist.llm.client import LlmClient
from coscientist.llm.types import Usage
from coscientist.profiles.load import load_profile
from tests.agents.test_adapter_roles import CassetteTransport, _exec_out_json
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.cli.test_journal import _FakeRuntime
from tests.cli.test_run_study import _default_tools
from tests.helpers import close_runtime as _close_runtime
from tests.helpers import open_runtime as _open_runtime
from tests.llm.test_client import FakeClock as LlmFakeClock
from tests.llm.test_client import _config, _ok_body

_PROFILE = load_profile("research", expected_tool_catalog_version=2)
_MODEL = _PROFILE.roles["planner"].model

_T = TypeVar("_T")

_NEED_OFF = Need(kind="need", needed=False, reason="off")


def _spec_json() -> str:
    return ResearchSpec(
        object="hyp lit study",
        goal=Goal(statement="find literature and hypothesize", research_form=None, trl=None),
        scope="synthetic",
        constraints=(Constraint(subtype="resource", content="cassette LLM", limit=None),),
        success_metrics=(SuccessMetric(metric="tasks", op="==", target="1", check="code", tolerance=None),),
    ).model_dump_json()


def _intent_json(
    tasks: tuple[TaskIntent, ...],
    *,
    hypotheses: Need = _NEED_OFF,
    literature: Need = _NEED_OFF,
    questions: tuple[QuestionIntent, ...] = (QuestionIntent(final_type="artifact", text="what holds?"),),
) -> str:
    return PlanIntent(
        tasks=tasks,
        questions=questions,
        hypotheses=hypotheses,
        literature=literature,
    ).model_dump_json()


def _hyp_out_json() -> str:
    return HypothesisOut(
        hypotheses=(
            HypothesisDraft(
                id="h1",
                statement="S1 raises yield",
                rationale="from the request",
                prediction="yield rises by >=10%",
                rank=0,
                question_ref="q1",
                selected=True,
            ),
            HypothesisDraft(
                id="h2",
                statement="S2 lowers yield",
                rationale="contradicts S1",
                prediction="yield drops by >=10%",
                rank=1,
                question_ref="q1",
                selected=False,
            ),
            HypothesisDraft(
                id="h3",
                statement="S3 has no effect",
                rationale="null outcome",
                prediction="abs(yield delta) < 1%",
                rank=2,
                question_ref="q1",
                selected=False,
            ),
        ),
    ).model_dump_json()


def _lit_out_json(*, tool: str, identity_key: str) -> str:
    return LiteratureOut(
        papers=(
            PaperRef(
                title="Sotorasib transforms the field",
                identity_key=identity_key,
                source_tool=tool,
                doi=identity_key,
                url=None,
            ),
        ),
    ).model_dump_json()


def _exec_out_no_tools_json() -> str:
    """Executor body for a plan task whose dispatch runs no tools."""
    return ExecutorRouteOut(summary="ran", values=(), tool_found=(), expansion_request=None).model_dump_json()


def _adapter(client: LlmClient, router: StaticToolRouter) -> LlmAgentAdapter:
    return LlmAgentAdapter(
        client,
        role_models={
            "planner": _MODEL,
            "executor-route": _MODEL,
            "tz_spec": _MODEL,
            "hypothesizer": _MODEL,
            "researcher": _MODEL,
        },
        tool_router=router,
        prompts=role_prompt_registry(),
        role_prompts={
            "planner": ROLE_PLANNER_REF,
            "executor-route": ROLE_EXECUTOR_REF,
            "tz_spec": ROLE_TZ_SPEC_REF,
            "hypothesizer": ROLE_HYPOTHESIZER_REF,
            "researcher": ROLE_RESEARCHER_REF,
        },
        clock=FakeClock(),
        hitl_policy=HitlPolicy(mode="headless"),
    )


def _adapter_with_critic(client: LlmClient, router: StaticToolRouter) -> LlmAgentAdapter:
    """_adapter + the wired plan critic (replan-round tests need CriticIn runs)."""
    return LlmAgentAdapter(
        client,
        role_models={
            "planner": _MODEL,
            "executor-route": _MODEL,
            "tz_spec": _MODEL,
            "hypothesizer": _MODEL,
            "researcher": _MODEL,
            "critic": _MODEL,
        },
        tool_router=router,
        prompts=role_prompt_registry(),
        role_prompts={
            "planner": ROLE_PLANNER_REF,
            "executor-route": ROLE_EXECUTOR_REF,
            "tz_spec": ROLE_TZ_SPEC_REF,
            "hypothesizer": ROLE_HYPOTHESIZER_REF,
            "researcher": ROLE_RESEARCHER_REF,
            "critic": ROLE_CRITIC_REF,
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


def _cassette_tool(result: dict[str, object]) -> dict[str, object]:
    return {"result": result, "args": {}}


def _lit_tools() -> dict[str, dict[str, object]]:
    tools = _default_tools()
    tools["search_papers"] = _cassette_tool(
        {"papers": [{"title": "Sotorasib transforms the field", "doi": "10.1/abc"}]}
    )
    tools["download_papers_from_search"] = _cassette_tool({"downloaded": 1})
    tools["find_papers_in_db"] = _cassette_tool({"matches": []})
    return tools


async def _run(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[bytes],
    wired_roles: frozenset[str],
    tools: StaticToolRouter | None = None,
    require_hypotheses: bool = False,
    study_id: str = "hyp_lit_study",
    loaded_profile: object = None,
    adapter: LlmAgentAdapter | None = None,
) -> tuple[int, dict[str, object]]:
    task = tmp_path / "t.txt"
    task.write_text("Study the sotorasib literature and hypothesize.", encoding="utf-8")
    router = tools if tools is not None else StaticToolRouter(tools=_lit_tools())
    if adapter is None:
        adapter = _adapter(_client(bodies, monkeypatch=monkeypatch), router)
    out = io.StringIO()
    code = await run_study(
        task_path=task,
        data_root=tmp_path / "data",
        deadline_s=3600.0,
        catalog_version=CATALOG_VERSION,
        catalog=minimal_test_catalog(),
        loaded_profile=loaded_profile if loaded_profile is not None else _PROFILE,
        owner="cli-test",
        adapter=adapter,
        out=out,
        study_id=study_id,
        require_hypotheses=require_hypotheses,
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
        wired_roles=wired_roles,
        study_clock=FakeClock(),
    )
    raw = out.getvalue().strip()
    if not raw:
        return code, {}
    loaded = json.loads(raw)
    assert isinstance(loaded, dict)
    return code, {str(k): v for k, v in loaded.items()}


async def _read_events(data_root: Path, study_id: str) -> list[StoredEvent]:
    rt = await _open_runtime(data_root, study_id, owner="cli-test-reader", acquire=False)
    try:
        events = await rt.store.read_all()
    finally:
        await _close_runtime(rt)
    return [ev for ev in events if isinstance(ev, StoredEvent)]


def _task_done_rows(events: list[StoredEvent], task_key: str) -> list[dict[str, object]]:
    rows = []
    for ev in events:
        if ev.type != "TaskDone" or not isinstance(ev.payload, dict):
            continue
        if str(ev.payload.get("task_id", "")).endswith(f"/{task_key}"):
            rows.append({str(k): v for k, v in ev.payload.items()})
    return rows


# --- wired happy paths --------------------------------------------------------


@pytest.mark.asyncio
async def test_wired_hypothesizer_closes_hyp_task_non_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HYP wired happy path: hypothesizer run journaled; HYP DAG task DONE SUCCESS."""
    plan = _intent_json(
        (
            TaskIntent(question_indices=(1,),
                goal="state hypotheses",
                route="hypotheses",
                key="HYP-0",
                tools_hint=("static:dataset_overview",),
            ),
        ),
        hypotheses=Need(kind="need", needed=True, reason="request asks for hypotheses"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
        _ok_body(content=_hyp_out_json()),
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
    ]
    code, summary = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        wired_roles=frozenset({"hypothesizer"}),
        require_hypotheses=True,
        study_id="wired_hyp_ok",
    )
    assert code == 0, "wired HYP happy path must fulfill"
    assert summary["verdict"] == "fulfilled"
    roles = [u["role"] for u in summary["usage"] if isinstance(u, dict)]
    assert "hypothesizer" in roles
    done = _task_done_rows(await _read_events(tmp_path / "data", "wired_hyp_ok"), "HYP")
    assert done, "intake HYP task never closed"
    assert done[-1]["outcome"] == "SUCCESS", f"HYP must close non-SKIPPED: {done[-1]!r}"
    # HYP v2: every hypothesis registered as a graph node (schema-validated attrs).
    events = await _read_events(tmp_path / "data", "wired_hyp_ok")
    hyp_nodes = [
        ev.payload
        for ev in events
        if ev.type == "GraphNodeUpserted"
        and isinstance(ev.payload, dict)
        and ev.payload.get("kind") == "hypothesis"
    ]
    assert len(hyp_nodes) == 3, f"expected 3 hypothesis nodes, got {len(hyp_nodes)}"
    attrs = [dict(n["attrs"]) for n in hyp_nodes]
    assert all(a.get("source_task") == "HYP" for a in attrs), attrs
    assert sum(1 for a in attrs if a.get("selected") is True) == 1, attrs
    assert {a.get("question_ref") for a in attrs} == {"q1"}, attrs
    # accept_plan materializes question_ref → addresses edges (hypothesis → q1).
    addr_edges = [
        ev.payload
        for ev in events
        if ev.type == "GraphEdgeAdded"
        and isinstance(ev.payload, dict)
        and ev.payload.get("kind") == "addresses"
    ]
    assert len(addr_edges) == 3, f"expected 3 addresses edges, got {len(addr_edges)}"
    assert all(str(e["dst_id"]).endswith(":question:q1") for e in addr_edges), addr_edges


@pytest.mark.asyncio
async def test_wired_researcher_closes_lit_task_non_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LIT wired happy path: fake literature surface; papers trace to tool output."""
    plan = _intent_json(
        (
            TaskIntent(question_indices=(1,),
                goal="overview", route="experiment", key="EXP-0", tools_hint=("static:dataset_overview",)
            ),
        ),
        literature=Need(kind="need", needed=True, reason="request needs literature"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
        _ok_body(content=_lit_out_json(tool="static:search_papers", identity_key="10.1/abc")),
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
    ]
    code, summary = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        wired_roles=frozenset({"researcher"}),
        study_id="wired_lit_ok",
    )
    assert code == 0, "wired LIT happy path must fulfill"
    assert summary["verdict"] == "fulfilled"
    lit_rows = [u for u in summary["usage"] if isinstance(u, dict) and u.get("role") == "researcher"]
    assert lit_rows, "no researcher usage row"
    assert lit_rows[0]["task_key"] == "LIT"
    done = _task_done_rows(await _read_events(tmp_path / "data", "wired_lit_ok"), "LIT")
    assert done, "intake LIT task never closed"
    assert done[-1]["outcome"] == "SUCCESS", f"LIT must close non-SKIPPED: {done[-1]!r}"


@pytest.mark.asyncio
async def test_replan_round_repeating_literature_need_not_re_driven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replan round re-requesting literature.needed does NOT re-drive the LIT stage.

    Round 1: LIT runs (researcher wired) and closes DONE SUCCESS; the critic
    orders a revise. Round 2: the planner intent sets literature.needed=true
    again — the stage is one-shot, so the round proceeds on the already
    journaled record (usage note), the journal keeps exactly one LIT close,
    and the study fulfills (previously: RuntimeError("task already DONE")).
    """
    plan = _intent_json(
        (
            TaskIntent(question_indices=(1,),
                goal="overview", route="experiment", key="EXP-0", tools_hint=("static:dataset_overview",)
            ),
        ),
        literature=Need(kind="need", needed=True, reason="request needs literature"),
    )
    critic_revise = CriticVerdict(
        decision="revise",
        issues=(CriticIssue(path="EXP-0", message="declare the expected output"),),
    ).model_dump_json()
    critic_accept = CriticVerdict(decision="accept", issues=()).model_dump_json()
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
        _ok_body(content=_lit_out_json(tool="static:search_papers", identity_key="10.1/abc")),
        _ok_body(content=critic_revise),
        _ok_body(content=plan),
        _ok_body(content=critic_accept),
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
    ]
    code, summary = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        wired_roles=frozenset({"researcher", "critic"}),
        study_id="replan_lit_once",
        adapter=_adapter_with_critic(
            _client(bodies, monkeypatch=monkeypatch), StaticToolRouter(tools=_lit_tools())
        ),
    )
    assert code == 0, "replan with a satisfied LIT stage must fulfill"
    assert summary["verdict"] == "fulfilled"
    lit_rows = [
        u
        for u in summary["usage"]
        if isinstance(u, dict) and u.get("role") == "researcher" and "prompt_tokens" in u
    ]
    assert len(lit_rows) == 1, f"LIT must run exactly once: {lit_rows!r}"
    notes = [u for u in summary["usage"] if isinstance(u, dict) and "one-shot" in str(u.get("note", ""))]
    assert len(notes) == 1, "the satisfied-stage skip must leave exactly one honest usage note"
    done = _task_done_rows(await _read_events(tmp_path / "data", "replan_lit_once"), "LIT")
    assert len(done) == 1 and done[0]["outcome"] == "SUCCESS", f"LIT closes once: {done!r}"


@pytest.mark.asyncio
async def test_replan_round_literature_flip_runs_stage_before_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """literature.needed false→true across replan rounds: the stage RUNS in round 2.

    Terminal DAG state is written from the ACCEPTED plan only: a round-1
    need-off leaves LIT startable (no pre-accept cancellation), so the round-2
    flip drives the wired researcher honestly and the study fulfills. The
    core's accept_plan closes stages the accepted plan does not need.
    """
    plan_off = _intent_json(
        (
            TaskIntent(question_indices=(1,),
                goal="overview", route="experiment", key="EXP-0", tools_hint=("static:dataset_overview",)
            ),
        ),
        literature=Need(kind="need", needed=False, reason="no literature requested"),
    )
    plan_on = _intent_json(
        (
            TaskIntent(question_indices=(1,),
                goal="overview", route="experiment", key="EXP-0", tools_hint=("static:dataset_overview",)
            ),
        ),
        literature=Need(kind="need", needed=True, reason="plan now needs literature"),
    )
    critic_revise = CriticVerdict(
        decision="revise",
        issues=(CriticIssue(path="EXP-0", message="declare the expected output"),),
    ).model_dump_json()
    critic_accept = CriticVerdict(decision="accept", issues=()).model_dump_json()
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan_off),
        _ok_body(content=critic_revise),
        _ok_body(content=plan_on),
        _ok_body(content=_lit_out_json(tool="static:search_papers", identity_key="10.1/abc")),
        _ok_body(content=critic_accept),
        _ok_body(content=_exec_out_json(tool="dataset_overview")),
    ]
    code, summary = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        wired_roles=frozenset({"researcher", "critic"}),
        study_id="replan_lit_flip_runs",
        adapter=_adapter_with_critic(
            _client(bodies, monkeypatch=monkeypatch), StaticToolRouter(tools=_lit_tools())
        ),
    )
    assert code == 0, "a need flip across rounds must drive the stage before accept"
    assert summary["verdict"] == "fulfilled"
    lit_rows = [
        u
        for u in summary["usage"]
        if isinstance(u, dict) and u.get("role") == "researcher" and "prompt_tokens" in u
    ]
    assert len(lit_rows) == 1, f"LIT must run exactly once (in round 2): {lit_rows!r}"
    done = _task_done_rows(await _read_events(tmp_path / "data", "replan_lit_flip_runs"), "LIT")
    assert len(done) == 1 and done[0]["outcome"] == "SUCCESS", f"LIT closes once: {done!r}"


# --- fail-closed refuses ------------------------------------------------------


@pytest.mark.asyncio
async def test_wired_researcher_invented_citation_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Papers must trace to journaled tool results: unknown source_tool → rc=4."""
    plan = _intent_json(
        (
            TaskIntent(question_indices=(1,),
                goal="overview", route="experiment", key="EXP-0", tools_hint=("static:dataset_overview",)
            ),
        ),
        literature=Need(kind="need", needed=True, reason="request needs literature"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
        # Cites a tool that was never called → record refuses (no invented citations).
        _ok_body(content=_lit_out_json(tool="static:find_papers_in_db", identity_key="10.1/abc")),
    ]
    with pytest.raises(CliRefuse, match="no invented citations") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            wired_roles=frozenset({"researcher"}),
            study_id="wired_lit_invented",
        )
    assert ei.value.code == 4
    events = await _read_events(tmp_path / "data", "wired_lit_invented")
    assert not _task_done_rows(events, "LIT"), "LIT must not close after a citation refuse"


@pytest.mark.asyncio
async def test_wired_researcher_identity_key_not_in_tool_result_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """identity_key absent from the cited tool's result JSON → rc=4 (fail-closed)."""
    plan = _intent_json(
        (
            TaskIntent(question_indices=(1,),
                goal="overview", route="experiment", key="EXP-0", tools_hint=("static:dataset_overview",)
            ),
        ),
        literature=Need(kind="need", needed=True, reason="request needs literature"),
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=plan),
        # The cited tool ran, but the identity key is not in its result bytes.
        _ok_body(content=_lit_out_json(tool="static:search_papers", identity_key="10.9999/fabricated")),
    ]
    with pytest.raises(CliRefuse, match="identity_key not found") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            wired_roles=frozenset({"researcher"}),
            study_id="wired_lit_key_drift",
        )
    assert ei.value.code == 4


@pytest.mark.asyncio
async def test_wired_researcher_without_literature_tools_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wired researcher + surface without the profile literature tools → refuse."""
    plan = _intent_json(
        (
            TaskIntent(question_indices=(1,),
                goal="overview", route="experiment", key="EXP-0", tools_hint=("static:dataset_overview",)
            ),
        ),
        literature=Need(kind="need", needed=True, reason="request needs literature"),
    )
    bodies = [_ok_body(content=_spec_json()), _ok_body(content=plan)]
    with pytest.raises(CliRefuse, match="not on ToolRouter surface") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            wired_roles=frozenset({"researcher"}),
            tools=StaticToolRouter(tools=_default_tools()),
            study_id="wired_lit_no_tools",
        )
    assert ei.value.code == 3


@pytest.mark.asyncio
async def test_wired_hypothesizer_without_profile_role_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wired hypothesizer but the profile lacks the role → code 5 (report-lane mirror)."""
    stripped = _PROFILE.model_copy(update={"roles": {
        k: v for k, v in _PROFILE.roles.items() if k != "hypothesizer"
    }})
    with pytest.raises(CliRefuse, match="has no hypothesizer role") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=[],
            wired_roles=frozenset({"hypothesizer"}),
            loaded_profile=stripped,
            study_id="wired_hyp_no_role",
        )
    assert ei.value.code == 5


@pytest.mark.asyncio
async def test_wired_researcher_route_drift_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wired researcher requires the literature route to wire exactly that role."""
    from coscientist.profiles.models import RouteConfig

    drifted_routes = dict(_PROFILE.routes)
    drifted_routes["literature"] = RouteConfig(
        role="planner", budget=drifted_routes["literature"].budget
    )
    drifted = _PROFILE.model_copy(update={"routes": drifted_routes})
    with pytest.raises(CliRefuse, match="route 'literature' role is 'planner'") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=[],
            wired_roles=frozenset({"researcher"}),
            loaded_profile=drifted,
            study_id="wired_lit_route_drift",
        )
    assert ei.value.code == 5


# --- journal-level unit tests (fake runtime, SPEC-record mirror) --------------


def _usage() -> Usage:
    return Usage(prompt_tokens=1, completion_tokens=1, latency_ms=1.0)


def _hyp_record_out() -> HypothesisOut:
    return HypothesisOut(
        hypotheses=(
            HypothesisDraft(
                id="h1", statement="s", rationale="r",
                prediction="p1", rank=0, question_ref="q1", selected=True,
            ),
            HypothesisDraft(
                id="h2", statement="s2", rationale="r2",
                prediction="p2", rank=1, question_ref="q1", selected=False,
            ),
            HypothesisDraft(
                id="h3", statement="s3", rationale="r3",
                prediction="p3", rank=2, question_ref="q1", selected=False,
            ),
        )
    )


def _lit_run(tool: str, result_json: str, *, out: LiteratureOut) -> AgentRun:
    return AgentRun(
        output=out,
        error=None,
        usage=_usage(),
        tool_calls=(
            ToolCallRef(
                tool_name=tool,
                resolution_status="tool_found",
                args=(),
                result_json=result_json,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_record_hypotheses_empty_expected_outputs_values_only() -> None:
    rt = _FakeRuntime(expected_outputs=[])
    await record_hypotheses(rt, "a1", _hyp_record_out())
    assert rt.executor is not None and len(rt.executor.records) == 1
    _aid, out = rt.executor.records[0]
    assert out.summary == "hypothesizer"
    assert out.values["hypotheses"][0]["id"] == "h1"
    assert out.outputs == {}


@pytest.mark.asyncio
async def test_record_hypotheses_expected_outputs_no_invented_names() -> None:
    """Artifact names come only from the journaled task's expected_outputs."""
    rt = _FakeRuntime()
    await record_hypotheses(rt, "a1", _hyp_record_out())
    assert rt.executor is not None
    _aid, out = rt.executor.records[0]
    assert set(out.outputs) == {"main"}
    assert [p["name"] for p in rt.executor.artifacts.puts] == ["main"]


@pytest.mark.asyncio
async def test_record_literature_tool_provenance_and_traceable_papers() -> None:
    out = LiteratureOut(
        papers=(
            PaperRef(title="t", identity_key="10.1/abc", source_tool="papers-search:search_papers"),
        )
    )
    rt = _FakeRuntime(expected_outputs=[])
    run = _lit_run(
        "papers-search:search_papers",
        json.dumps({"papers": [{"title": "t", "doi": "10.1/abc"}]}),
        out=out,
    )
    await record_literature(rt, "a1", run)
    assert rt.executor is not None
    _aid, recorded = rt.executor.records[0]
    assert recorded.summary == "researcher"
    assert set(recorded.outputs) == {"papers-search:search_papers"}
    assert recorded.claims == []
    puts = rt.executor.artifacts.puts
    assert len(puts) == 1 and puts[0]["provenance"] == "tool"


@pytest.mark.asyncio
async def test_record_literature_citation_without_tool_result_refuses() -> None:
    out = LiteratureOut(
        papers=(
            PaperRef(title="t", identity_key="10.1/abc", source_tool="papers-search:search_papers"),
        )
    )
    rt = _FakeRuntime(expected_outputs=[])
    run = _lit_run(
        "papers-search:find_papers_in_db",
        json.dumps({"papers": [{"title": "t", "doi": "10.1/abc"}]}),
        out=out,
    )
    with pytest.raises(CliRefuse, match="no invented citations"):
        await record_literature(rt, "a1", run)


@pytest.mark.asyncio
async def test_record_literature_identity_key_must_appear_in_result() -> None:
    out = LiteratureOut(
        papers=(
            PaperRef(
                title="t", identity_key="10.9999/fabricated", source_tool="papers-search:search_papers"
            ),
        )
    )
    rt = _FakeRuntime(expected_outputs=[])
    run = _lit_run(
        "papers-search:search_papers",
        json.dumps({"papers": [{"title": "t", "doi": "10.1/abc"}]}),
        out=out,
    )
    with pytest.raises(CliRefuse, match="identity_key not found") as ei:
        await record_literature(rt, "a1", run)
    # The refuse carries receipt facts (keys the tool surfaced + cited keys)
    # so the automatic loop's repair round can fix the output targeted.
    assert "10.1/abc" in str(ei.value)
    assert "10.9999/fabricated" in str(ei.value)


@pytest.mark.asyncio
async def test_record_literature_non_literature_out_refuses() -> None:
    rt = _FakeRuntime(expected_outputs=[])
    run = AgentRun(
        output=None,
        error=None,
        usage=_usage(),
        tool_calls=(),
    )
    with pytest.raises(CliRefuse, match="researcher must return LiteratureOut"):
        await record_literature(rt, "a1", run)
