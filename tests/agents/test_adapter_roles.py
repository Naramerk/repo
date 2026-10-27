"""Fake LLM + concrete MCP ToolRouter for planner / executor-route (no real keys)."""

from __future__ import annotations

import json
import random
import sys
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

import pytest
from pydantic import ValidationError

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.critic import CriticVerdict
from coscientist.agents.prompts.registry import PromptRef
from coscientist.agents.prompts.roles import (
    ROLE_CRITIC_REF,
    ROLE_EXECUTOR_REF,
    ROLE_PLANNER_REF,
    ROLE_REVIEWER_REF,
    ROLE_TZ_SPEC_REF,
    role_prompt_registry,
)
from coscientist.agents.schemas import (
    AgentError,
    ExecutorRouteIn,
    ExecutorRouteOut,
    OutputSpecDraft,
    PlanIntent,
    PlannedToolCall,
    PlanOut,
    QuestionIntent,
    RubricDraft,
    RubricItemDraft,
    StudyContext,
    TaskIntent,
    TaskSpecDraft,
    ToolArg,
    ToolCallRef,
)
from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.domain import Budget, HitlPolicy, Need
from coscientist.llm.client import LlmClient
from coscientist.llm.transport import TransportResponse
from coscientist.mcp.config import StdioServerConfig
from coscientist.tools.contract import CallCtx, ToolOutput, ToolSpec
from coscientist.tools.mcp_adapter import McpToolProfile
from coscientist.tools.router import McpServerRoute, ToolNeed, ToolRouter, ToolRouterConfig
from tests.llm.test_client import FakeClock as LlmFakeClock
from tests.llm.test_client import _config, _ok_body
from tests.mcp.conftest import make_client, make_config
from tests.mcp.fake_server import FakeStdioServer, Handler

_ROLE_PROMPTS: dict[str, PromptRef] = {
    "planner": ROLE_PLANNER_REF,
    "executor-route": ROLE_EXECUTOR_REF,
    "tz_spec": ROLE_TZ_SPEC_REF,
}

_SERVER = "agent-tools"
_TOOL = "dataset_overview"
_BOUND = f"{_SERVER}:{_TOOL}"

_T = TypeVar("_T")


def _budget(
    *,
    max_llm_calls: int = 10,
    max_tokens: int = 100_000,
    max_tool_calls: int = 50,
) -> Budget:
    return Budget(
        max_llm_calls=max_llm_calls,
        max_tokens=max_tokens,
        max_tool_calls=max_tool_calls,
    )


@dataclass(frozen=True)
class _NamedTool:
    spec: ToolSpec

    async def call(self, args: dict[str, object], ctx: CallCtx) -> ToolOutput:
        del args, ctx
        raise AssertionError("NamedTool.call must not be invoked; use ToolRouter.call")


class _BadSpecName:
    @property
    def name(self) -> object:
        return None


class _BadNamedTool:
    @property
    def spec(self) -> _BadSpecName:
        return _BadSpecName()


class _NotBudget:
    """Non-Budget stand-in for wrong-type budget checks."""


def _named_tools(*names: str) -> list[_NamedTool]:
    return [_NamedTool(ToolSpec(name=n)) for n in names]


def _tool_profile() -> McpToolProfile:
    return McpToolProfile(effect="read", is_fetch=False, idempotent=True, max_retries=0)


def _idle_router(*, tool: str = _TOOL) -> ToolRouter:
    """Configured but not opened — ``resolve`` raises ToolRouterError('not open')."""
    return ToolRouter(ToolRouterConfig(servers=(McpServerRoute(server_name=_SERVER, tools={tool: _tool_profile()}),)))


class _NoFetch:
    async def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> bytes:
        raise AssertionError("no fetch")


def _dataset_handler(method: str, params: dict[str, object] | None) -> dict[str, object] | None:
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
    handler: Handler = _dataset_handler,
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
        self.requests: list[dict[str, object]] = []

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


def _task_draft(*, tool: str = _BOUND) -> TaskSpecDraft:
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
                rubric=_artifact_rubric(),
            ),
        ),
        criteria=("artifact_present",),
    )


def _route_in(
    *,
    tool: str = _BOUND,
    args: tuple[ToolArg, ...] = (ToolArg(key="limit", value_json="2"),),
) -> ExecutorRouteIn:
    return ExecutorRouteIn(
        task=_task_draft(tool=tool),
        study_id="s1",
        attempt_id="a1",
        tool_args=(PlannedToolCall(tool_name=tool, args=args),),
    )


_INTENT_TOOLS = (_BOUND, f"{_SERVER}:chemical_space_clustering")


def _plan_out_json() -> str:
    """Planner cassette body: typed PlanIntent (frozen-plan internals derived)."""
    intent = PlanIntent(
        tasks=(
            TaskIntent(question_indices=(1,), goal="overview", route="experiment", key="EXP-0", tools_hint=(_BOUND,)),
            TaskIntent(
                question_indices=(1,),
                goal="cluster",
                route="experiment",
                key="EXP-1",
                tools_hint=(f"{_SERVER}:chemical_space_clustering",),
            ),
        ),
        questions=(QuestionIntent(final_type="artifact", text="two tasks?"),),
        hypotheses=Need(kind="need", needed=False, reason="not required for two_tasks"),
        literature=Need(kind="need", needed=False, reason="not required for two_tasks"),
    )
    return intent.model_dump_json()


def _intent_ctx() -> StudyContext:
    return StudyContext(study_id="s1", request="two tasks", profile="research", tool_ids=_INTENT_TOOLS)


def _exec_out_json(*, tool: str) -> str:
    return ExecutorRouteOut(
        summary=f"ran {tool}",
        values=(
            ToolArg(key="ok", value_json="true"),
            # Explicit expected-output materialization for CLI journal (no sole-tool guess).
            ToolArg(key="main", value_json='{"ok":true}'),
        ),
        tool_found=(tool,),
        # Required-nullable (strict structured outputs): explicit None.
        expansion_request=None,
    ).model_dump_json()


def _client(bodies: list[bytes], *, monkeypatch: pytest.MonkeyPatch) -> LlmClient:
    return _client_and_transport(bodies, monkeypatch=monkeypatch)[0]


def _client_and_transport(
    bodies: list[bytes], *, monkeypatch: pytest.MonkeyPatch
) -> tuple[LlmClient, CassetteTransport]:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    clock = LlmFakeClock()

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable: Awaitable[_T], *, timeout_s: float) -> _T:
        del timeout_s
        return await awaitable

    transport = CassetteTransport(bodies)
    return (
        LlmClient(
            _config(max_retries=0),
            transport,
            clock=clock,
            sleep=sleep,
            wait_for=wait_for,
            rng=random.Random(0),
        ),
        transport,
    )


def _adapter(
    client: LlmClient,
    *,
    router: ToolRouter,
    clock: FakeClock,
    hitl_policy: HitlPolicy,
    extra_roles: frozenset[str] = frozenset(),
) -> LlmAgentAdapter:
    role_models = {
        "planner": "test-model",
        "executor-route": "test-model",
        "tz_spec": "test-model",
    }
    role_prompts = dict(_ROLE_PROMPTS)
    if "critic" in extra_roles:
        role_models["critic"] = "test-model"
        role_prompts["critic"] = ROLE_CRITIC_REF
    if "reviewer" in extra_roles:
        role_models["reviewer"] = "test-model"
        role_prompts["reviewer"] = ROLE_REVIEWER_REF
    adapter = LlmAgentAdapter(
        client,
        role_models=role_models,
        tool_router=router,
        prompts=role_prompt_registry(),
        role_prompts=role_prompts,
        clock=clock,
        hitl_policy=hitl_policy,
    )

    class _UnitExecutor:
        async def check_active(self, study_id: str, attempt_id: str) -> None:
            pass

        async def invoke(self, name: str, args: dict[str, object], ctx: CallCtx):
            resolved = router.resolve(ToolNeed(kind="by_name", value=name))
            output = await router.call(resolved.server_name, resolved.tool_name, args, ctx)
            try:
                result = json.dumps(output.data)
            except (TypeError, ValueError) as exc:
                raise AgentError("tool result is not JSON-serializable") from exc
            return ToolCallRef(tool_name=name, resolution_status="tool_found", args=(), result_json=result)

    adapter.bind_tool_executor(_UnitExecutor())
    return adapter


@pytest.mark.asyncio
async def test_planner_structured_plan_out_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(
        [_ok_body(content=_plan_out_json(), prompt_tokens=11, completion_tokens=7)],
        monkeypatch=monkeypatch,
    )
    adapter = _adapter(
        client,
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    run = await adapter.run(
        "planner",
        _intent_ctx(),
        [],
        _budget(),
    )
    assert isinstance(run.output, PlanIntent)
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
        [_ok_body(content=_exec_out_json(tool=_BOUND), prompt_tokens=5, completion_tokens=3)],
        monkeypatch=monkeypatch,
    )
    calls: list[tuple[str, str, dict[str, object]]] = []
    async with _bound_router() as router:
        orig_call = router.call

        async def _track(
            server_name: str,
            tool_name: str,
            args: dict[str, object],
            ctx: CallCtx,
        ) -> ToolOutput:
            calls.append((server_name, tool_name, dict(args)))
            return await orig_call(server_name, tool_name, args, ctx)

        monkeypatch.setattr(router, "call", _track)
        adapter = _adapter(
            client,
            router=router,
            clock=FakeClock(),
            hitl_policy=HitlPolicy(),
        )
        run = await adapter.run(
            "executor-route",
            _route_in(),
            _named_tools(_BOUND),
            _budget(),
        )
    assert calls == [(_SERVER, _TOOL, {"limit": 2})]
    assert run.tool_calls[0].resolution_status == "tool_found"
    assert run.tool_calls[0].tool_name == _BOUND
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

    async def wait_for(awaitable: Awaitable[_T], *, timeout_s: float) -> _T:
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
    adapter = _adapter(
        client,
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    await adapter.run(
        "planner",
        _intent_ctx(),
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
        LlmAgentAdapter(
            _client([], monkeypatch=monkeypatch),
            role_models={"executor-route": "m", "tz_spec": "m"},
            tool_router=_idle_router(),
            prompts=role_prompt_registry(),
            role_prompts=dict(_ROLE_PROMPTS),
            clock=FakeClock(),
            hitl_policy=HitlPolicy(),
        )


def test_role_models_empty_model_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AgentError, match="non-empty str"):
        LlmAgentAdapter(
            _client([], monkeypatch=monkeypatch),
            role_models={"planner": " ", "executor-route": "m", "tz_spec": "m"},
            tool_router=_idle_router(),
            prompts=role_prompt_registry(),
            role_prompts=dict(_ROLE_PROMPTS),
            clock=FakeClock(),
            hitl_policy=HitlPolicy(),
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
            hitl_policy=HitlPolicy(),
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
            hitl_policy=HitlPolicy(),
        )


def test_hitl_policy_wrong_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AgentError, match="hitl_policy must be HitlPolicy"):
        LlmAgentAdapter(
            _client([], monkeypatch=monkeypatch),
            role_models={"planner": "m", "executor-route": "m", "tz_spec": "m"},
            tool_router=_idle_router(),
            prompts=role_prompt_registry(),
            role_prompts=dict(_ROLE_PROMPTS),
            clock=FakeClock(),
            hitl_policy=object(),  # runtime refuse: must be HitlPolicy
        )


@pytest.mark.asyncio
async def test_unsupported_role_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([_ok_body(content=_plan_out_json())], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="unsupported role"):
        await adapter.run(
            "teleporter",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(),
        )


@pytest.mark.asyncio
async def test_critic_and_reviewer_input_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wired critic/reviewer parse their typed inputs; wrong input → typed AgentError."""
    from coscientist.agents.schemas import CriticIn

    adapter = _adapter(
        _client(
            [_ok_body(content=CriticVerdict(decision="accept", issues=()).model_dump_json())], monkeypatch=monkeypatch
        ),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
        extra_roles=frozenset({"critic"}),
    )
    ctx = StudyContext(study_id="s1", request="two tasks", profile="research")
    plan = PlanOut(
        tasks=(
            TaskSpecDraft(
                task_key="EXP-0",
                title="overview",
                goal="overview",
                route="experiment",
                depends_on=(),
                addresses=("q1",),
                tools_hint=(),
                expected_outputs=(),
                criteria=(),
            ),
        ),
        questions=(),
        hypotheses=Need(kind="need", needed=False, reason="off"),
        literature=Need(kind="need", needed=False, reason="off"),
    )
    run = await adapter.run("critic", CriticIn(context=ctx, plan=plan), [], _budget())
    assert isinstance(run.output, CriticVerdict)
    assert run.output.decision == "accept"
    with pytest.raises(AgentError, match="invalid CriticIn"):
        await adapter.run("critic", {"plan": "not-a-CriticIn"}, [], _budget())


@pytest.mark.asyncio
async def test_schema_role_without_instance_config_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Class schema exists (critic) but the adapter lacks the role model/prompt."""
    adapter = _adapter(
        _client([_ok_body(content=_plan_out_json())], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="not configured on this adapter"):
        # Input is never parsed: the configuration refuse fires first.
        await adapter.run(
            "critic",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(),
        )


@pytest.mark.asyncio
async def test_invalid_llm_json_raises_agent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client(
            [_ok_body(content="{not-json"), _ok_body(content="{also-not-json")],
            monkeypatch=monkeypatch,
        ),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="after repair retry"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(),
        )


@pytest.mark.asyncio
async def test_schema_repair_retry_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    client, transport = _client_and_transport(
        [_ok_body(content="{not-json"), _ok_body(content=_plan_out_json())],
        monkeypatch=monkeypatch,
    )
    adapter = _adapter(client, router=_idle_router(), clock=FakeClock(), hitl_policy=HitlPolicy())
    run = await adapter.run(
        "planner",
        _intent_ctx(),
        [],
        _budget(),
    )
    assert run.error is None
    assert isinstance(run.output, PlanIntent)
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_invalid_input_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="invalid StudyContext"):
        await adapter.run("planner", {"study_id": 1, "request": "r", "profile": "p"}, [], _budget())


@pytest.mark.asyncio
async def test_wrong_basemodel_input_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="expected StudyContext"):
        await adapter.run(
            "planner",
            PlanIntent(
                tasks=(),
                questions=(),
                hypotheses=Need(kind="need", needed=False, reason="off"),
                literature=Need(kind="need", needed=False, reason="off"),
            ),
            [],
            _budget(),
        )


@pytest.mark.asyncio
async def test_idle_router_resolve_not_open_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="not open"):
        await adapter.run(
            "executor-route",
            _route_in(),
            _named_tools(_BOUND),
            _budget(),
        )


@pytest.mark.asyncio
async def test_budget_wrong_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="must be Budget"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _NotBudget(),  # runtime refuse: must be Budget
        )


@pytest.mark.asyncio
async def test_budget_max_cost_rub_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="max_cost_rub"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            Budget(
                max_llm_calls=10,
                max_tokens=100_000,
                max_cost_rub=Decimal("1"),
                max_tool_calls=50,
                deadline=None,
            ),
        )


@pytest.mark.asyncio
async def test_tools_not_list_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
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
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="spec.name"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [_BadNamedTool()],
            _budget(),
        )


@pytest.mark.asyncio
async def test_tool_arg_invalid_json_raises() -> None:
    with pytest.raises(ValidationError, match="value_json must be valid JSON"):
        _route_in(args=(ToolArg(key="limit", value_json="not-json"),))


@pytest.mark.asyncio
async def test_schema_repair_refused_when_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, transport = _client_and_transport(
        [_ok_body(content="{not-json"), _ok_body(content=_plan_out_json())],
        monkeypatch=monkeypatch,
    )
    adapter = _adapter(client, router=_idle_router(), clock=FakeClock(), hitl_policy=HitlPolicy())
    with pytest.raises(AgentError, match="after repair retry"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(max_llm_calls=1),
        )
    assert len(transport.requests) == 1  # no repair call past the budget


def test_study_context_carries_criterion_template_ids() -> None:
    ctx = StudyContext(study_id="s", request="r", profile="p", criterion_template_ids=("a", "b"))
    assert ctx.criterion_template_ids == ("a", "b")
    assert StudyContext(study_id="s", request="r", profile="p").criterion_template_ids == ()


def test_study_context_carries_tool_ids() -> None:
    ctx = StudyContext(study_id="s", request="r", profile="p", tool_ids=("a:b", "static:x"))
    assert ctx.tool_ids == ("a:b", "static:x")


def test_tool_arg_accepts_object_value() -> None:
    arg = ToolArg(key="static:ping", value_json={"pong": True})  # type: ignore[arg-type]
    assert arg.value_json == '{"pong":true}'


@pytest.mark.asyncio
async def test_tool_not_in_allow_list_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="allow-list"):
        await adapter.run(
            "executor-route",
            _route_in(),
            _named_tools(f"{_SERVER}:other_tool"),
            _budget(),
        )


@pytest.mark.asyncio
async def test_empty_allow_list_with_tools_hint_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="tools list is empty"):
        await adapter.run(
            "executor-route",
            _route_in(),
            [],
            _budget(),
        )


@pytest.mark.asyncio
async def test_budget_max_tool_calls_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="max_tool_calls"):
        await adapter.run(
            "executor-route",
            _route_in(),
            _named_tools(_BOUND),
            _budget(max_tool_calls=0),
        )


@pytest.mark.asyncio
async def test_budget_max_llm_calls_below_one_raises_before_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="max_llm_calls must be >= 1"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(max_llm_calls=0),
        )


@pytest.mark.asyncio
async def test_budget_max_tokens_below_one_raises_before_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="max_tokens must be >= 1"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(max_tokens=0),
        )


@pytest.mark.asyncio
async def test_budget_naive_deadline_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="timezone-aware"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            Budget(
                max_llm_calls=10,
                max_tokens=100_000,
                max_cost_rub=None,
                max_tool_calls=50,
                deadline=datetime(2026, 1, 1),
            ),
        )


@pytest.mark.asyncio
async def test_budget_deadline_exceeded_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    wall = FakeClock(start=datetime(2026, 6, 1, tzinfo=UTC))
    adapter = _adapter(
        _client([], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=wall,
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="budget.deadline exceeded"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            Budget(
                max_llm_calls=10,
                max_tokens=100_000,
                max_cost_rub=None,
                max_tool_calls=50,
                deadline=wall.now(),
            ),
        )


@pytest.mark.asyncio
async def test_budget_max_tokens_exceeded_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        _client(
            [_ok_body(content=_plan_out_json(), prompt_tokens=80, completion_tokens=30)],
            monkeypatch=monkeypatch,
        ),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    with pytest.raises(AgentError, match="max_tokens exceeded"):
        await adapter.run(
            "planner",
            StudyContext(study_id="s", request="r", profile="p"),
            [],
            _budget(max_tokens=50),
        )


@pytest.mark.asyncio
async def test_budget_llm_calls_accumulate_across_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two successful ``run``s consume the shared adapter counter; third refuses."""
    bodies = [
        _ok_body(content=_plan_out_json(), prompt_tokens=1, completion_tokens=1),
        _ok_body(content=_plan_out_json(), prompt_tokens=1, completion_tokens=1),
        _ok_body(content=_plan_out_json(), prompt_tokens=1, completion_tokens=1),
    ]
    adapter = _adapter(
        _client(bodies, monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    budget = _budget(max_llm_calls=2, max_tokens=100_000)
    ctx = _intent_ctx()
    await adapter.run("planner", ctx, [], budget)
    await adapter.run("planner", ctx, [], budget)
    assert adapter._llm_calls_used == 2
    with pytest.raises(AgentError, match="max_llm_calls exceeded"):
        await adapter.run("planner", ctx, [], budget)


@pytest.mark.asyncio
async def test_budget_tokens_accumulate_across_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token usage sums across ``run``s; exceeding the shared limit raises."""
    bodies = [
        _ok_body(content=_plan_out_json(), prompt_tokens=40, completion_tokens=10),
        _ok_body(content=_plan_out_json(), prompt_tokens=40, completion_tokens=10),
    ]
    adapter = _adapter(
        _client(bodies, monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    budget = _budget(max_llm_calls=10, max_tokens=90)
    ctx = _intent_ctx()
    await adapter.run("planner", ctx, [], budget)
    assert adapter._tokens_used == 50
    with pytest.raises(AgentError, match="max_tokens exceeded"):
        await adapter.run("planner", ctx, [], budget)


@pytest.mark.asyncio
async def test_tool_result_not_json_serializable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _bound_router() as router:

        async def _bad_call(
            server_name: str,
            tool_name: str,
            args: dict[str, object],
            ctx: CallCtx,
        ) -> ToolOutput:
            del server_name, tool_name, args, ctx
            return ToolOutput(data={"x": object()})

        monkeypatch.setattr(router, "call", _bad_call)
        adapter = _adapter(
            _client([], monkeypatch=monkeypatch),
            router=router,
            clock=FakeClock(),
            hitl_policy=HitlPolicy(),
        )
        with pytest.raises(AgentError, match="JSON-serializable"):
            await adapter.run(
                "executor-route",
                _route_in(),
                _named_tools(_BOUND),
                _budget(),
            )


def test_executor_route_out_schema_is_strict_closed() -> None:
    schema = ExecutorRouteOut.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["values"]["type"] == "array"
    entry = schema["$defs"]["ToolArg"]
    assert entry["additionalProperties"] is False
    assert "value_json" in entry["properties"]
    # OpenAI strict structured outputs: EVERY property must be listed in
    # required — an optional field is required + nullable (anyOf … null).
    # A defaulted field (property outside required) would make the first live
    # strict executor-route request reject on the response schema.
    assert set(schema["required"]) == set(schema["properties"])
    expansion = schema["properties"]["expansion_request"]
    assert {"type": "null"} in expansion["anyOf"]
    assert {"$ref": "#/$defs/ExpansionRequest"} in expansion["anyOf"]
    # KNOWN GAP (delta review C; see docs/plan/adaptive-plan-runtime.md):
    # strict applies RECURSIVELY at every object level, and the nested
    # $defs graph pulled in by the $ref (ExpansionRequest → TaskSpec →
    # OutputSpec/Rubric/RequirementRef/…) has properties OUTSIDE required —
    # a strict provider still rejects the executor-route response_format
    # (the schema regressed for strict providers vs 2e6363d, where the only
    # $def was the all-required ToolArg). This pin documents the limitation
    # and must be flipped by the recursive-strictification slice.
    nested = schema["$defs"]["ExpansionRequest"]
    assert set(nested["properties"]) - set(nested.get("required", []))


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
    adapter = _adapter(
        _client([_ok_body(content=spec.model_dump_json())], monkeypatch=monkeypatch),
        router=_idle_router(),
        clock=FakeClock(),
        hitl_policy=HitlPolicy(),
    )
    run = await adapter.run(
        "tz_spec",
        StudyContext(study_id="s1", request="two tasks", profile="research"),
        [],
        _budget(),
    )
    assert isinstance(run.output, ResearchSpec)
    assert run.output.object == "catalyst X"
    assert run.error is None


@pytest.mark.asyncio
async def test_role_request_carries_schema_in_system_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Providers that ignore ``response_format`` (GLM JSON mode) still see the schema.

    The system message must embed the role output JSON schema verbatim; the
    wire ``response_format`` stays attached and client-side validation is the
    only acceptance gate.
    """
    from coscientist.agents.research_spec import Goal, ResearchSpec

    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "test-key-not-real")
    spec = ResearchSpec(
        object="o",
        goal=Goal(statement="g", research_form=None, trl=None),
        scope="s",
        constraints=(),
        success_metrics=(),
    )
    transport = CassetteTransport([_ok_body(content=spec.model_dump_json())])

    async def sleep(_delay: float) -> None:
        return None

    async def wait_for(awaitable: Awaitable[object], *, timeout_s: float) -> object:
        del timeout_s
        return await awaitable

    client = LlmClient(
        _config(max_retries=0),
        transport,
        clock=LlmFakeClock(),
        sleep=sleep,
        wait_for=wait_for,
        rng=random.Random(0),
    )
    adapter = _adapter(client, router=_idle_router(), clock=FakeClock(), hitl_policy=HitlPolicy())
    await adapter.run(
        "tz_spec",
        StudyContext(study_id="s1", request="r", profile="research"),
        [],
        _budget(),
    )
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request["response_format"]["type"] == "json_schema"
    system_message = request["messages"][0]
    assert system_message["role"] == "system"
    schema = ResearchSpec.model_json_schema()
    assert json.dumps(schema, ensure_ascii=False, separators=(",", ":")) in system_message["content"]
    assert '"success_metrics"' in system_message["content"]


@pytest.mark.asyncio
async def test_planner_request_schema_enumerates_context_tool_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tools_hint is schema-checked against ctx.tool_ids (no prompt rule)."""
    client, transport = _client_and_transport([_ok_body(content=_plan_out_json())], monkeypatch=monkeypatch)
    adapter = _adapter(client, router=_idle_router(), clock=FakeClock(), hitl_policy=HitlPolicy())
    await adapter.run("planner", _intent_ctx(), [], _budget())
    schema = transport.requests[0]["response_format"]["json_schema"]["schema"]
    task_defs = schema["$defs"]
    tool_enum = None
    for defn in task_defs.values():
        props = defn.get("properties", {})
        if "tools_hint" in props:
            tool_enum = props["tools_hint"].get("items", {}).get("enum")
    assert sorted(tool_enum or []) == sorted(_INTENT_TOOLS)


@pytest.mark.asyncio
async def test_planner_tool_hint_outside_context_tool_ids_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = PlanIntent(
        tasks=(TaskIntent(question_indices=(1,), goal="g", route="experiment", tools_hint=("static:not-a-tool",)),),
        questions=(QuestionIntent(final_type="artifact", text="q?"),),
        hypotheses=Need(kind="need", needed=False, reason="off"),
        literature=Need(kind="need", needed=False, reason="off"),
    ).model_dump_json()
    client, transport = _client_and_transport([_ok_body(content=bad), _ok_body(content=bad)], monkeypatch=monkeypatch)
    adapter = _adapter(client, router=_idle_router(), clock=FakeClock(), hitl_policy=HitlPolicy())
    with pytest.raises(AgentError, match="after repair retry"):
        await adapter.run("planner", _intent_ctx(), [], _budget())
    assert len(transport.requests) == 2  # one repair round against the enum


def test_planner_prompt_body_has_no_plan_contract_block() -> None:
    reg = role_prompt_registry()
    body = reg.render(ROLE_PLANNER_REF, {"role": "planner"})
    assert "PLAN CONTRACT" not in body
    assert body.startswith("You are the planner role.")


def test_plan_intent_single_tool_id_schema_is_enum_not_const() -> None:
    """Single-id Literal serializes as const; providers may ignore const (review LOW)."""
    from coscientist.agents.schemas import plan_intent_model_for

    model = plan_intent_model_for(("agent-tools:only_tool",))
    task_defs = model.model_json_schema()["$defs"]
    hint = next(d["properties"]["tools_hint"] for d in task_defs.values() if "tools_hint" in d.get("properties", {}))
    assert hint["items"] == {"enum": ["agent-tools:only_tool"], "type": "string"}
