"""A2/A4: JournaledAgentPort — fake LLM, journal events, replay without re-call."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from coscientist.agents import (
    AgentBudgetLimits,
    AgentConfig,
    AgentDecision,
    JournaledAgentPort,
    RoleConfig,
    ToolCallRequest,
    llm_calls_from_events,
)
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ExpansionRequest,
    HitlPolicy,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.execution.tool_registry import (
    RegisteredToolSpec,
    _register_tool_for_tests,
    _reset_registry_for_tests,
)
from coscientist.llm.types import LlmRequest, LlmResponse, Usage
from coscientist.tools.contract import CallCtx, ToolOutput, ToolSpec
from tests.helpers import close_runtime, open_runtime


class _TaskInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    query: str


class _Mono:
    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t


class FakeLlm:
    complete_timeout_s = 1.0

    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)
        self.calls = 0

    async def complete(self, request: LlmRequest, *, timeout_s: float) -> LlmResponse:
        if timeout_s <= 0.0:
            raise AssertionError("timeout_s must be > 0")
        if not self._contents:
            raise AssertionError("FakeLlm called with no remaining scripted responses")
        self.calls += 1
        content = self._contents.pop(0)
        return LlmResponse(
            content=content,
            model=request.model,
            finish_reason="stop",
            usage=Usage(prompt_tokens=3, completion_tokens=2, latency_ms=1.5),
        )


class _LocalEcho:
    def __init__(self) -> None:
        self.spec = ToolSpec(name="local_echo", provider="local", effect="read")
        self.calls: list[dict[str, Any]] = []

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
        self.calls.append(dict(args))
        return ToolOutput(data={"echo": args.get("text")})


def _decision_tool(name: str, arguments: dict[str, Any]) -> str:
    return AgentDecision(
        tool_calls=(ToolCallRequest(name=name, arguments=arguments),),
        final=None,
    ).model_dump_json()


def _decision_final(summary: str) -> str:
    return AgentDecision(
        tool_calls=(),
        final=AgentTaskOutput(summary=summary),
    ).model_dump_json()


def _agent_config(*, tools: tuple[str, ...] = ()) -> AgentConfig:
    return AgentConfig(
        roles={
            "executor": RoleConfig(
                model="test-model",
                system_prompt_ref="exec_sys",
                max_turns=4,
                allowed_tool_names=tools,
            )
        },
        prompts={"exec_sys": "Return AgentDecision JSON only."},
        cost_per_prompt_token_rub=None,
        cost_per_completion_token_rub=None,
    )


def _budget(**overrides: Any) -> AgentBudgetLimits:
    base: dict[str, Any] = {
        "max_llm_calls": 10,
        "max_tokens": 10000,
        "max_tool_calls": 10,
        "max_cost_rub": None,
    }
    base.update(overrides)
    return AgentBudgetLimits.model_validate(base)


@pytest.fixture(autouse=True)
def _registry():
    _reset_registry_for_tests()
    _register_tool_for_tests(
        RegisteredToolSpec(name="local_echo", effect="read", provider="local")
    )
    yield
    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_llm_call_and_usage_recorded(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_agent_llm")
    await rt.create_study(
        profile="research",
        request="agent llm",
        catalog_version=3,
        sid="st_agent_llm",
        finalize_reserve_s=5,
    )
    arts = LocalArtifactStore(tmp_path / "cas")
    fake = FakeLlm([_decision_final("done")])
    port = JournaledAgentPort(
        config=_agent_config(),
        llm=fake,
        writer=rt.writer,
        artifacts=arts,
        clock=rt.clock,
        study_id="st_agent_llm",
        attempt_id="att#1",
        task_key="EXP-0",
        hitl_policy=HitlPolicy(),
        on_event=rt._project_local,
        monotonic=_Mono(),
    )
    try:
        run = await port.run(
            "executor",
            _TaskInput(query="q"),
            tools=[],
            budget=_budget(),
        )
        assert run.status == "ok"
        assert run.output is not None
        assert run.output.summary == "done"
        assert fake.calls == 1
        events = await rt.store.read_all()
        types = [e.type for e in events]
        assert "LlmCallRecorded" in types
        assert "UsageRecorded" in types
        llm_ev = next(e for e in events if e.type == "LlmCallRecorded")
        assert llm_ev.payload["model"] == "test-model"
        assert llm_ev.payload["prompt_ref"] == "exec_sys"
        assert llm_ev.payload["prompt_tokens"] == 3
        assert llm_ev.payload["completion_tokens"] == 2
        assert llm_ev.payload["from_replay"] is False
        assert llm_ev.schema_version == 1
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_replay_no_llm_recall(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_agent_replay")
    await rt.create_study(
        profile="research",
        request="replay",
        catalog_version=3,
        sid="st_agent_replay",
        finalize_reserve_s=5,
    )
    arts = LocalArtifactStore(tmp_path / "cas")
    fake1 = FakeLlm([_decision_final("from-live")])
    port1 = JournaledAgentPort(
        config=_agent_config(),
        llm=fake1,
        writer=rt.writer,
        artifacts=arts,
        clock=rt.clock,
        study_id="st_agent_replay",
        attempt_id="att#1",
        task_key="EXP-0",
        hitl_policy=HitlPolicy(),
        on_event=rt._project_local,
        monotonic=_Mono(),
    )
    try:
        run1 = await port1.run(
            "executor", _TaskInput(query="q"), tools=[], budget=_budget()
        )
        assert run1.status == "ok"
        assert fake1.calls == 1
        events = await rt.store.read_all()
        lookup = llm_calls_from_events(events)

        fake2 = FakeLlm([])
        port2 = JournaledAgentPort(
            config=_agent_config(),
            llm=fake2,
            writer=rt.writer,
            artifacts=arts,
            clock=rt.clock,
            study_id="st_agent_replay",
            attempt_id="att#1",
            task_key="EXP-0",
            hitl_policy=HitlPolicy(),
            on_event=rt._project_local,
            llm_call_lookup=lookup.get,
            monotonic=_Mono(),
        )
        run2 = await port2.run(
            "executor", _TaskInput(query="q"), tools=[], budget=_budget()
        )
        assert run2.status == "ok"
        assert run2.output is not None
        assert run2.output.summary == "from-live"
        assert fake2.calls == 0
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_tool_loop_records_tool_called(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_agent_tool")
    await rt.create_study(
        profile="research",
        request="tools",
        catalog_version=3,
        sid="st_agent_tool",
        finalize_reserve_s=5,
    )
    arts = LocalArtifactStore(tmp_path / "cas")
    echo = _LocalEcho()
    fake = FakeLlm(
        [
            _decision_tool("local_echo", {"text": "hi"}),
            _decision_final("after-tool"),
        ]
    )
    port = JournaledAgentPort(
        config=_agent_config(tools=("local_echo",)),
        llm=fake,
        writer=rt.writer,
        artifacts=arts,
        clock=rt.clock,
        study_id="st_agent_tool",
        attempt_id="att#1",
        task_key="EXP-0",
        hitl_policy=HitlPolicy(),
        on_event=rt._project_local,
        monotonic=_Mono(),
    )
    try:
        run = await port.run(
            "executor",
            _TaskInput(query="q"),
            tools=[echo],
            budget=_budget(),
        )
        assert run.status == "ok"
        assert echo.calls == [{"text": "hi"}]
        assert len(run.tool_calls) == 1
        events = await rt.store.read_all()
        tool_evs = [e for e in events if e.type == "ToolCalled"]
        assert len(tool_evs) == 1
        assert tool_evs[0].payload["tool"] == "local_echo"
        assert tool_evs[0].payload["ok"] is True
        assert tool_evs[0].payload["provider"] == "local"
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_budget_exceeded_max_llm_calls(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_agent_bud")
    await rt.create_study(
        profile="research",
        request="budget",
        catalog_version=3,
        sid="st_agent_bud",
        finalize_reserve_s=5,
    )
    arts = LocalArtifactStore(tmp_path / "cas")
    fake = FakeLlm([_decision_final("nope")])
    port = JournaledAgentPort(
        config=_agent_config(),
        llm=fake,
        writer=rt.writer,
        artifacts=arts,
        clock=rt.clock,
        study_id="st_agent_bud",
        attempt_id="att#1",
        task_key="EXP-0",
        hitl_policy=HitlPolicy(),
        on_event=rt._project_local,
        monotonic=_Mono(),
    )
    try:
        run = await port.run(
            "executor",
            _TaskInput(query="q"),
            tools=[],
            budget=_budget(max_llm_calls=0),
        )
        assert run.status == "budget_exceeded"
        assert fake.calls == 0
        assert run.output is None
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_expansion_request_journaled(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_agent_exp")
    await rt.create_study(
        profile="research",
        request="expand",
        catalog_version=3,
        sid="st_agent_exp",
        finalize_reserve_s=5,
    )
    arts = LocalArtifactStore(tmp_path / "cas")
    exp = ExpansionRequest(
        tasks=[
            TaskSpec(
                task_key="EXP-new",
                title="child",
                goal="do child",
                route="experiment",
                depends_on=["EXP-0"],
                criteria=[RequirementRef(template_id="always_pass")],
            )
        ],
        reason="need child",
    )
    final = AgentDecision(
        tool_calls=(),
        final=AgentTaskOutput(summary="ask expand", expansion_request=exp),
    ).model_dump_json()
    fake = FakeLlm([final])
    port = JournaledAgentPort(
        config=_agent_config(),
        llm=fake,
        writer=rt.writer,
        artifacts=arts,
        clock=rt.clock,
        study_id="st_agent_exp",
        attempt_id="att#1",
        task_key="EXP-0",
        hitl_policy=HitlPolicy(),
        on_event=rt._project_local,
        monotonic=_Mono(),
    )
    try:
        run = await port.run(
            "executor", _TaskInput(query="q"), tools=[], budget=_budget()
        )
        assert run.status == "ok"
        assert run.output is not None
        assert run.output.expansion_request is not None
        events = await rt.store.read_all()
        assert any(e.type == "PlanExpansionRequested" for e in events)
    finally:
        await close_runtime(rt)


def test_agent_config_refuses_missing_prompt() -> None:
    with pytest.raises(ValidationError):
        AgentConfig(
            roles={
                "r": RoleConfig(
                    model="m",
                    system_prompt_ref="missing",
                    max_turns=1,
                    allowed_tool_names=(),
                )
            },
            prompts={"other": "x"},
            cost_per_prompt_token_rub=None,
            cost_per_completion_token_rub=None,
        )


def test_agent_decision_xor() -> None:
    with pytest.raises(ValidationError):
        AgentDecision(tool_calls=(), final=None)
    with pytest.raises(ValidationError):
        AgentDecision(
            tool_calls=(ToolCallRequest(name="t", arguments={}),),
            final=AgentTaskOutput(summary="x"),
        )
