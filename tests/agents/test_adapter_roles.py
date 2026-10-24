"""Fake LLM + FakeToolRouter for planner / executor-route (no network, no keys)."""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from typing import Any

import pytest

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.schemas import (
    ExecutorRouteIn,
    ExecutorRouteOut,
    OutputSpecDraft,
    PlanOut,
    QuestionDraft,
    StudyContext,
    TaskSpecDraft,
)
from coscientist.core.execution.domain import Need
from coscientist.llm.client import LlmClient
from coscientist.llm.transport import TransportResponse
from coscientist.tools.router import Resolution, ToolNeed
from tests.llm.test_client import FakeClock, _config, _ok_body


class FakeToolRouter:
    def __init__(self, *, tools: Mapping[str, Mapping[str, Any]]) -> None:
        self.tools = dict(tools)
        self.calls: list[str] = []

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
        del args, study_id, task_key, attempt_id
        self.calls.append(tool_name)
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
        values={"ok": True},
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


@pytest.mark.asyncio
async def test_planner_structured_plan_out_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(
        [_ok_body(content=_plan_out_json(), prompt_tokens=11, completion_tokens=7)], monkeypatch=monkeypatch
    )
    adapter = LlmAgentAdapter(
        client,
        role_models={"planner": "test-model", "executor-route": "test-model"},
        tool_router=FakeToolRouter(tools={}),
    )
    run = await adapter.run(
        "planner",
        StudyContext(study_id="s1", request="two tasks", profile="research"),
        [],
        None,
    )
    assert isinstance(run.output, PlanOut)
    assert len(run.output.tasks) == 2
    assert run.usage.prompt_tokens == 11
    assert run.usage.completion_tokens == 7
    assert run.usage.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_executor_route_uses_tool_router(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(
        [_ok_body(content=_exec_out_json(tool="dataset_overview"), prompt_tokens=5, completion_tokens=3)],
        monkeypatch=monkeypatch,
    )
    router = FakeToolRouter(tools={"dataset_overview": {"rows": 2}})
    adapter = LlmAgentAdapter(
        client,
        role_models={"planner": "test-model", "executor-route": "test-model"},
        tool_router=router,
    )
    draft = TaskSpecDraft(
        task_key="EXP-0",
        title="t",
        goal="g",
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
    )
    run = await adapter.run(
        "executor-route",
        ExecutorRouteIn(
            task=draft,
            dependency_context={"study_id": "s1", "attempt_id": "a1"},
        ),
        [],
        None,
    )
    assert router.calls == ["dataset_overview"]
    assert run.tool_calls[0].resolution_status == "tool_found"
    assert isinstance(run.output, ExecutorRouteOut)
    assert run.usage.prompt_tokens == 5


@pytest.mark.asyncio
async def test_structured_request_includes_response_format(
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
    adapter = LlmAgentAdapter(
        client,
        role_models={"planner": "m", "executor-route": "m"},
        tool_router=FakeToolRouter(tools={}),
    )
    await adapter.run(
        "planner",
        StudyContext(study_id="s", request="r", profile="p"),
        [],
        None,
    )
    assert transport.requests[0]["response_format"]["type"] == "json_schema"
