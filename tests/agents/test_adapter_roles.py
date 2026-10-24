"""Fake LLM + concrete MCP ToolRouter for planner / executor-route (no real keys)."""

from __future__ import annotations

import json
import random
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.prompts.registry import PromptRef
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
from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.domain import Budget, Need
from coscientist.llm.client import LlmClient
from coscientist.llm.transport import TransportResponse
from coscientist.mcp.config import StdioServerConfig
from coscientist.tools.contract import CallCtx, ToolOutput, ToolSpec
from coscientist.tools.mcp_adapter import McpToolProfile
from coscientist.tools.router import McpServerRoute, ToolRouter, ToolRouterConfig
from tests.llm.test_client import FakeClock as LlmFakeClock
from tests.llm.test_client import _config, _ok_body
from tests.mcp.conftest import make_client, make_config
from tests.mcp.fake_server import FakeStdioServer

_ROLE_PROMPTS = {
    "planner": ROLE_PLANNER_REF,
    "executor-route": ROLE_EXECUTOR_REF,
    "tz_spec": ROLE_TZ_SPEC_REF,
}

_SERVER = "agent-tools"
_TOOL = "dataset_overview"


def _budget(
    *,
    max_llm_calls: int = 10,
    max_tokens: int = 100_000,
    max_tool_calls: int = 50,
    deadline: datetime | None = None,
) -> Budget:
    return Budget(
        max_llm_calls=max_llm_calls,
        max_tokens=max_tokens,
        max_cost_rub=None,
        max_tool_calls=max_tool_calls,
        deadline=deadline,
    )


@dataclass(frozen=True)
class _NamedTool:
    spec: ToolSpec

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
        del args, ctx
        raise AssertionError("NamedTool.call must not be invoked; use ToolRouter.call")


def _named_tools(*names: str) -> list[_NamedTool]:
    return [_NamedTool(ToolSpec(name=n)) for n in names]


def _tool_profile() -> McpToolProfile:
    return McpToolProfile(effect="read", is_fetch=False, idempotent=True, max_retries=0)


def _idle_router(*, tool: str = _TOOL) -> ToolRouter:
    """Configured but not opened — ``resolve`` → tool_missing (fail-closed)."""
    return ToolRouter(
        ToolRouterConfig(
            servers=(McpServerRoute(server_name=_SERVER, tools={tool: _tool_profile()}),)
        )
    )


class _NoFetch:
    async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
        raise AssertionError("no fetch")


def _dataset_handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
    if method == "initialize":
        assert params is not None
        return {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0.0.1"},
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": _TOOL,
                    "description": "dataset overview",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"limit": {"type": "integer"}},
                    },
                }
            ]
        }
    if method == "tools/call":
        assert params is not None
        return {
            "content": [{"type": "text", "text": json.dumps({"rows": 2})}],
            "isError": False,
        }
    raise AssertionError(f"unexpected method {method}")


@asynccontextmanager
async def _bound_router(
    handler: Any = _dataset_handler,
    *,
    tool: str = _TOOL,
) -> AsyncIterator[ToolRouter]:
    router = _idle_router(tool=tool)
    client = make_client(
        make_config(
            servers=(
                StdioServerConfig(
                    transport="stdio",
                    name=_SERVER,
                    command=sys.executable,
                    args=("-c", "pass"),
                    env_keys=(),
                ),
            )
        ),
        file_fetcher=_NoFetch(),
    )
    async with FakeStdioServer(handler) as server:
        session = await client.connect_memory(_SERVER, server.client)
        await router.attach_session(session, server_name=_SERVER)
        try:
            yield router
        finally:
            await router.aclose()


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
        questions=(QuestionDraft(id="Q1", text="two tasks?", final_type="artifact"),),
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
    clock = LlmFakeClock()

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable: Any, *, timeout_s: float) -> Any:
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
    router: ToolRouter | None = None,
    role_models: Mapping[str, str] | None = None,
    role_prompts: Mapping[str, PromptRef] | None = None,
    clock: FakeClock | None = None,
) -> LlmAgentAdapter:
    if role_models is None:
        models = {"planner": "test-model", "executor-route": "test-model", "tz_spec": "test-model"}
    else:
        models = dict(role_models)
    if role_prompts is None:
        prompts_map = dict(_ROLE_PROMPTS)
    else:
        prompts_map = dict(role_prompts)
    if router is None:
        tool_router: ToolRouter = _idle_router()
    else:
        tool_router = router
    if clock is None:
        wall: FakeClock = FakeClock()
    else:
        wall = clock
    return LlmAgentAdapter(
        client,
        role_models=models,
        tool_router=tool_router,
        prompts=role_prompt_registry(),
        role_prompts=prompts_map,
        clock=wall,
    )


@pytest.mark.asyncio
async def test_planner_structured_plan_out_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(
        [_ok_body(content=_plan_out_json(), prompt_tokens=11, completion_tokens=7)],
        monkeypatch=monkeypatch,
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
    calls: list[tuple[str, str, dict[str, Any]]] = []
    async with _bound_router() as router:
        orig_call = router.call

        async def _track(
            server_name: str,
            tool_name: str,
            args: dict[str, Any],
            ctx: CallCtx,
        ) -> ToolOutput:
            calls.append((server_name, tool_name, dict(args)))
            return await orig_call(server_name, tool_name, args, ctx)

        monkeypatch.setattr(router, "call", _track)
        adapter = _adapter(client, router=router)
        run = await adapter.run(
            "executor-route",
            _route_in(),
            _named_tools("dataset_overview"),
            _budget(),
        )
    assert calls == [(_SERVER, _TOOL, {"limit": 2})]
    assert run.tool_calls[0].resolution_status == "tool_found"
    assert run.tool_calls[0].tool_name == f"{_SERVER}:{_TOOL}"
    assert "rows" in run.tool_calls[0].result_json
    assert run.error is None
    assert isinstance(run.output, ExecutorRouteOut)
    assert run.usage.prompt_tokens == 5


@pytest.mark.asyncio
async def test_structured_request_includes_response_format_and_prompt_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = CassetteTransport([_ok_body(content=_plan_out_json())])
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    clock = LlmFakeClock()

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable: Any, *, timeout_s: float) -> Any:
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
    assert "schema" in body["response_format"]["json_schema"]
    system = body["messages"][0]["content"]
    assert "planner" in system
    assert "{{" not in system


def test_role_models_missing_planner_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AgentError, match="role_models must include"):
        _adapter(
            _client([], monkeypatch=monkeypatch),
            role_models={"executor-route": "m", "tz_spec": "m"},
        )


def test_role_models_empty_model_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AgentError, match="non-empty str"):
        _adapter(
            _client([], monkeypatch=monkeypatch),
            role_models={"planner": " ", "executor-route": "m", "tz_spec": "m"},
        )


def test_role_prompts_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AgentError, match="role_prompts must include"):
        LlmAgentAdapter(
            _client([], monkeypatch=monkeypatch),
            role_models={"planner": "m", "executor-route": "m", "tz_spec": "m"},
            tool_router=_idle_router(),
            prompts=role_prompt_registry(),
            role_prompts={"planner": ROLE_PLANNER_REF, "executor-route": ROLE_EXECUTOR_REF},
            clock=FakeClock(),
        )


def test_role_prompts_non_prompt_ref_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_prompts: Mapping[str, object] = {
        "planner": "not-a-ref",
        "executor-route": ROLE_EXECUTOR_REF,
        "tz_spec": ROLE_TZ_SPEC_REF,
    }
    with pytest.raises(AgentError, match="must be PromptRef"):
        LlmAgentAdapter(
            _client([], monkeypatch=monkeypatch),
            role_models={"planner": "m", "executor-route": "m", "tz_spec": "m"},
            tool_router=_idle_router(),
            prompts=role_prompt_registry(),
            role_prompts=bad_prompts,  # runtime refuse: values must be PromptRef
            clock=FakeClock(),
        )


@pytest.mark.asyncio
async def test_unsupported_role_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([_ok_body(content=_plan_out_json())], monkeypatch=monkeypatch))
    with pytest.raises(AgentError, match="unsupported role"):
        await adapter.run(
            "critic",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(),
        )


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
async def test_wrong_basemodel_input_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([], monkeypatch=monkeypatch))
    with pytest.raises(AgentError, match="expected StudyContext"):
        await adapter.run(
            "planner",
            PlanOut(
                tasks=(),
                questions=(),
                hypotheses=Need(kind="need", needed=False, reason="off"),
                literature=Need(kind="need", needed=False, reason="off"),
            ),
            [],
            _budget(),
        )


@pytest.mark.asyncio
async def test_tool_missing_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
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
async def test_budget_wrong_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([], monkeypatch=monkeypatch))
    with pytest.raises(AgentError, match="must be Budget"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            SimpleNamespace(max_llm_calls=1),
        )


@pytest.mark.asyncio
async def test_tools_not_list_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([], monkeypatch=monkeypatch))
    bad_tools: object = ("not", "a", "list")
    with pytest.raises(AgentError, match="tools must be a list"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            bad_tools,  # runtime refuse: must be list[Tool]
            _budget(),
        )


@pytest.mark.asyncio
async def test_tools_missing_spec_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([], monkeypatch=monkeypatch))
    with pytest.raises(AgentError, match="spec.name"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [SimpleNamespace(spec=SimpleNamespace(name=None))],
            _budget(),
        )


@pytest.mark.asyncio
async def test_tool_arg_invalid_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _bound_router() as router:
        adapter = _adapter(_client([], monkeypatch=monkeypatch), router=router)
        with pytest.raises(AgentError, match="value_json is not valid JSON"):
            await adapter.run(
                "executor-route",
                _route_in(args=(ToolArg(key="limit", value_json="not-json"),)),
                _named_tools("dataset_overview"),
                _budget(),
            )


@pytest.mark.asyncio
async def test_tool_not_in_allow_list_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([], monkeypatch=monkeypatch), router=_idle_router())
    with pytest.raises(AgentError, match="allow-list"):
        await adapter.run(
            "executor-route",
            _route_in(),
            _named_tools("other_tool"),
            _budget(),
        )


@pytest.mark.asyncio
async def test_empty_allow_list_with_tools_hint_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([], monkeypatch=monkeypatch), router=_idle_router())
    with pytest.raises(AgentError, match="tools list is empty"):
        await adapter.run(
            "executor-route",
            _route_in(),
            [],
            _budget(),
        )


@pytest.mark.asyncio
async def test_budget_max_tool_calls_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([], monkeypatch=monkeypatch), router=_idle_router())
    with pytest.raises(AgentError, match="max_tool_calls"):
        await adapter.run(
            "executor-route",
            _route_in(),
            _named_tools("dataset_overview"),
            _budget(max_tool_calls=0),
        )


@pytest.mark.asyncio
async def test_budget_max_llm_calls_zero_raises_before_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(_client([], monkeypatch=monkeypatch))
    with pytest.raises(AgentError, match="max_llm_calls exceeded"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(max_llm_calls=0),
        )


@pytest.mark.asyncio
async def test_budget_max_llm_calls_counts_across_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(
        _client(
            [
                _ok_body(content=_plan_out_json()),
                _ok_body(content=_plan_out_json()),
            ],
            monkeypatch=monkeypatch,
        )
    )
    ctx = StudyContext(study_id="s", request="r", profile="p")
    first = await adapter.run("planner", ctx, [], _budget(max_llm_calls=1))
    assert first.error is None
    with pytest.raises(AgentError, match="max_llm_calls exceeded"):
        await adapter.run("planner", ctx, [], _budget(max_llm_calls=1))


@pytest.mark.asyncio
async def test_budget_naive_deadline_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(_client([], monkeypatch=monkeypatch))
    with pytest.raises(AgentError, match="timezone-aware"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(deadline=datetime(2026, 1, 1)),
        )


@pytest.mark.asyncio
async def test_budget_deadline_exceeded_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    wall = FakeClock(start=datetime(2026, 6, 1, tzinfo=UTC))
    adapter = _adapter(_client([], monkeypatch=monkeypatch), clock=wall)
    with pytest.raises(AgentError, match="budget.deadline exceeded"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(deadline=wall.now()),
        )


@pytest.mark.asyncio
async def test_budget_max_tokens_exceeded_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client(
            [_ok_body(content=_plan_out_json(), prompt_tokens=80, completion_tokens=30)],
            monkeypatch=monkeypatch,
        )
    )
    with pytest.raises(AgentError, match="max_tokens exceeded"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(max_tokens=50),
        )


@pytest.mark.asyncio
async def test_tool_result_not_json_serializable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _bound_router() as router:

        async def _bad_call(
            server_name: str,
            tool_name: str,
            args: dict[str, Any],
            ctx: CallCtx,
        ) -> ToolOutput:
            del server_name, tool_name, args, ctx
            return ToolOutput(data={"x": object()})

        monkeypatch.setattr(router, "call", _bad_call)
        adapter = _adapter(_client([], monkeypatch=monkeypatch), router=router)
        with pytest.raises(AgentError, match="JSON-serializable"):
            await adapter.run(
                "executor-route",
                _route_in(),
                _named_tools("dataset_overview"),
                _budget(),
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


def test_json_schema_response_format_is_typed() -> None:
    from coscientist.llm.types import ChatMessage, JsonSchemaResponseFormat, StructuredLlmRequest

    fmt = JsonSchemaResponseFormat(name="planner", strict=True, schema={"type": "object"})
    req = StructuredLlmRequest(
        model="m",
        messages=[ChatMessage(role="user", content="x")],
        response_format=fmt,
    )
    payload = req.response_format.to_openai_payload()
    assert payload == {
        "type": "json_schema",
        "json_schema": {"name": "planner", "strict": True, "schema": {"type": "object"}},
    }


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
