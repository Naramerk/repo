"""Fake LLM + FakeToolRouter for planner / executor-route (no network, no keys)."""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.prompts.roles import (
    ROLE_EXECUTOR_REF,
    ROLE_PLANNER_REF,
    ROLE_TZ_SPEC_REF,
    role_prompt_registry,
)
from coscientist.agents.schemas import (
    AgentError,
    ExecutorRouteIn,
    ExecutorRouteOut,
    JsonEntry,
    OutputSpecDraft,
    PlannedToolCall,
    PlanOut,
    QuestionDraft,
    StudyContext,
    TaskSpecDraft,
    ToolArg,
)
from coscientist.core.execution.domain import Budget, Need
from coscientist.llm.client import LlmClient
from coscientist.llm.transport import TransportResponse
from coscientist.tools.resolve import Resolution, ToolNeed
from tests.llm.test_client import FakeClock, _config, _ok_body

_ROLE_PROMPTS = {"planner": ROLE_PLANNER_REF, "executor-route": ROLE_EXECUTOR_REF, "tz_spec": ROLE_TZ_SPEC_REF}


def _budget(*, max_llm_calls: int = 10, max_tokens: int = 100_000, max_tool_calls: int = 50) -> Budget:
    return Budget(
        max_llm_calls=max_llm_calls,
        max_tokens=max_tokens,
        max_cost_rub=None,
        max_tool_calls=max_tool_calls,
        deadline=None,
    )


def _named_tools(*names: str) -> list[Any]:
    return [SimpleNamespace(spec=SimpleNamespace(name=n)) for n in names]


class FakeToolRouter:
    def __init__(self, *, tools: Mapping[str, Mapping[str, Any]]) -> None:
        self.tools = dict(tools)
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def resolve(self, need: ToolNeed) -> Resolution:
        if need.kind == "by_name" and need.value in self.tools:
            return Resolution(status="tool_found", tool_name=need.value)
        return Resolution(status="tool_missing", tool_name=None)

    async def call(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        *,
        study_id: str,
        task_key: str,
        attempt_id: str,
    ) -> Mapping[str, Any]:
        del study_id, task_key, attempt_id
        self.calls.append((tool_name, dict(args)))
        return dict(self.tools[tool_name])


class CassetteTransport:
    """Replay canned OpenAI-compatible bodies (CI cassette; no real keys)."""

    def __init__(self, bodies: list[bytes]) -> None:
        self._bodies = list(bodies)
        self.requests: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_s: float,
    ) -> TransportResponse:
        del method, url, headers, timeout_s
        self.requests.append(json.loads(body.decode("utf-8")))
        if not self._bodies:
            raise AssertionError("cassette exhausted")
        return TransportResponse(status_code=200, headers={}, body=self._bodies.pop(0))


def _task_draft(*, tool: str = "dataset_overview") -> TaskSpecDraft:
    return TaskSpecDraft(
        task_key="EXP-0",
        title="t",
        goal="g",
        route="experiment",
        depends_on=(),
        addresses=("Q1",),
        tools_hint=(tool,),
        expected_outputs=(
            OutputSpecDraft(
                name="main",
                media_type="application/json",
                schema_ref=None,
                required=True,
            ),
        ),
        criteria=("artifact_present",),
    )


def _route_in(
    *,
    tool: str = "dataset_overview",
    args: tuple[ToolArg, ...] = (ToolArg(key="limit", value_json="2"),),
) -> ExecutorRouteIn:
    return ExecutorRouteIn(
        task=_task_draft(tool=tool),
        study_id="s1",
        attempt_id="a1",
        tool_args=(PlannedToolCall(tool_name=tool, args=args),),
    )


def _plan_out_json() -> str:
    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="Dataset overview",
                goal="overview",
                route="experiment",
                depends_on=(),
                addresses=("Q1",),
                tools_hint=("dataset_overview",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                    ),
                ),
                criteria=("artifact_present",),
            ),
            TaskSpecDraft(
                task_key="EXP-1",
                title="Chemical space clustering",
                goal="cluster",
                route="experiment",
                depends_on=(),
                addresses=("Q1",),
                tools_hint=("chemical_space_clustering",),
                expected_outputs=(
                    OutputSpecDraft(
                        name="main",
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                    ),
                ),
                criteria=("artifact_present",),
            ),
        ),
        questions=(QuestionDraft(id="Q1", text="two tasks?", final_type="verdict"),),
        hypotheses=Need(kind="need", needed=False, reason="not required for two_tasks"),
        literature=Need(kind="need", needed=False, reason="not required for two_tasks"),
    )
    return plan.model_dump_json()


def _exec_out_json(*, tool: str) -> str:
    return ExecutorRouteOut(
        summary=f"ran {tool}",
        values=(JsonEntry(key="ok", value_json="true"),),
        tool_found=(tool,),
    ).model_dump_json()


def _client(bodies: list[bytes], *, monkeypatch: pytest.MonkeyPatch) -> LlmClient:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    clock = FakeClock()

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable, *, timeout_s: float):
        del timeout_s
        return await awaitable

    return LlmClient(
        _config(max_retries=0),
        CassetteTransport(bodies),
        clock=clock,
        sleep=sleep,
        wait_for=wait_for,
        rng=random.Random(0),
    )


def _adapter(
    client: LlmClient,
    *,
    router: FakeToolRouter | None = None,
    role_models: Mapping[str, str] | None = None,
) -> LlmAgentAdapter:
    return LlmAgentAdapter(
        client,
        role_models=dict(
            role_models or {"planner": "test-model", "executor-route": "test-model", "tz_spec": "test-model"}
        ),
        tool_router=router or FakeToolRouter(tools={}),
        prompts=role_prompt_registry(),
        role_prompts=_ROLE_PROMPTS,
    )


@pytest.mark.asyncio
async def test_planner_structured_plan_out_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(
        [_ok_body(content=_plan_out_json(), prompt_tokens=11, completion_tokens=7)], monkeypatch=monkeypatch
    )
    adapter = _adapter(client)
    run = await adapter.run(
        "planner",
        StudyContext(study_id="s1", request="two tasks", profile="research"),
        [],
        _budget(),
    )
    assert isinstance(run.output, PlanOut)
    assert run.error is None
    assert len(run.output.tasks) == 2
    assert run.usage.prompt_tokens == 11
    assert run.usage.completion_tokens == 7
    assert run.usage.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_executor_route_uses_tool_router_and_passes_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(
        [_ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=5, completion_tokens=3)],
        monkeypatch=monkeypatch,
    )
    router = FakeToolRouter(tools={"dataset_overview": {"rows": 2}})
    adapter = _adapter(client, router=router)
    run = await adapter.run(
        "executor-route",
        _route_in(),
        _named_tools("dataset_overview"),
        _budget(),
    )
    assert router.calls == [("dataset_overview", {"limit": 2})]
    assert run.tool_calls[0].resolution_status == "tool_found"
    assert run.tool_calls[0].result_json == '{"rows":2}'
    assert run.error is None
    assert isinstance(run.output, ExecutorRouteOut)
    assert run.usage.prompt_tokens == 5


@pytest.mark.asyncio
async def test_structured_request_includes_response_format_and_prompt_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = CassetteTransport([_ok_body(content=_plan_out_json())])
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    clock = FakeClock()

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable, *, timeout_s: float):
        del timeout_s
        return await awaitable

    client = LlmClient(
        _config(max_retries=0),
        transport,
        clock=clock,
        sleep=sleep,
        wait_for=wait_for,
        rng=random.Random(0),
    )
    adapter = _adapter(client)
    await adapter.run(
        "planner",
        StudyContext(study_id="s", request="r", profile="p"),
        [],
        _budget(),
    )
    body = transport.requests[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    system = body["messages"][0]["content"]
    assert "planner" in system
    assert "{{" not in system


def test_role_models_missing_planner_raises() -> None:
    with pytest.raises(AgentError, match="role_models must include"):
        LlmAgentAdapter(
            client=SimpleNamespace(),  # type: ignore[arg-type]
            role_models={"executor-route": "m", "tz_spec": "m"},
            tool_router=FakeToolRouter(tools={}),
            prompts=role_prompt_registry(),
            role_prompts=_ROLE_PROMPTS,
        )


def test_role_models_empty_model_raises() -> None:
    with pytest.raises(AgentError, match="non-empty str"):
        LlmAgentAdapter(
            client=SimpleNamespace(),  # type: ignore[arg-type]
            role_models={"planner": " ", "executor-route": "m", "tz_spec": "m"},
            tool_router=FakeToolRouter(tools={}),
            prompts=role_prompt_registry(),
            role_prompts=_ROLE_PROMPTS,
        )


@pytest.mark.asyncio
async def test_unsupported_role_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([_ok_body(content=_plan_out_json())], monkeypatch=monkeypatch))
    with pytest.raises(AgentError, match="unsupported role"):
        await adapter.run("critic", StudyContext(study_id="s", request="r", profile="p"), [], _budget())


@pytest.mark.asyncio
async def test_invalid_llm_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([_ok_body(content="{not-json")], monkeypatch=monkeypatch))
    with pytest.raises(AgentError, match="schema validation"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(),
        )


@pytest.mark.asyncio
async def test_invalid_input_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([], monkeypatch=monkeypatch))
    with pytest.raises(AgentError, match="invalid StudyContext"):
        await adapter.run("planner", {"study_id": 1, "request": "r", "profile": "p"}, [], _budget())


@pytest.mark.asyncio
async def test_tool_missing_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=FakeToolRouter(tools={}),
    )
    with pytest.raises(AgentError, match="fail-closed"):
        await adapter.run(
            "executor-route",
            _route_in(),
            _named_tools("dataset_overview"),
            _budget(),
        )


@pytest.mark.asyncio
async def test_budget_none_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([], monkeypatch=monkeypatch))
    with pytest.raises(AgentError, match="budget is required"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            None,
        )


@pytest.mark.asyncio
async def test_tool_not_in_allow_list_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=FakeToolRouter(tools={"dataset_overview": {"ok": True}}),
    )
    with pytest.raises(AgentError, match="allow-list"):
        await adapter.run(
            "executor-route",
            _route_in(),
            _named_tools("other_tool"),
            _budget(),
        )


@pytest.mark.asyncio
async def test_budget_max_tool_calls_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=FakeToolRouter(tools={"dataset_overview": {"ok": True}}),
    )
    with pytest.raises(AgentError, match="max_tool_calls"):
        await adapter.run(
            "executor-route",
            _route_in(),
            _named_tools("dataset_overview"),
            _budget(max_tool_calls=0),
        )


def test_executor_route_out_schema_is_strict_closed() -> None:
    schema = ExecutorRouteOut.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["values"]["type"] == "array"
    entry = schema["$defs"]["JsonEntry"]
    assert entry["additionalProperties"] is False
    assert "value_json" in entry["properties"]


def test_agent_run_has_error_field() -> None:
    from coscientist.agents.schemas import AgentRun
    from coscientist.llm.types import Usage

    run = AgentRun(
        output=None,
        error="boom",
        usage=Usage(prompt_tokens=0, completion_tokens=0, latency_ms=0.0),
        tool_calls=(),
    )
    assert run.error == "boom"


@pytest.mark.asyncio
async def test_tz_spec_returns_research_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    from coscientist.agents.research_spec import Constraint, Goal, ResearchSpec, SuccessMetric

    spec = ResearchSpec(
        object="catalyst X",
        goal=Goal(statement="measure X under Y", research_form=None, trl=None),
        scope="lab",
        constraints=(Constraint(subtype="resource", content="max 10 LLM calls", limit=None),),
        success_metrics=(SuccessMetric(metric="sources", op=">=", target="5", check="code", tolerance=None),),
    )
    adapter = _adapter(_client([_ok_body(content=spec.model_dump_json())], monkeypatch=monkeypatch))
    run = await adapter.run(
        "tz_spec",
        StudyContext(study_id="s1", request="two tasks", profile="research"),
        [],
        _budget(),
    )
    assert isinstance(run.output, ResearchSpec)
    assert run.output.object == "catalyst X"
    assert run.error is None
