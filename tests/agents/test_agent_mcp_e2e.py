"""E2E: fake LLM + fake MCP server → journal Llm/Tool/Mcp events."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from coscientist.agents import (
    AgentBudgetLimits,
    AgentConfig,
    AgentDecision,
    JournaledAgentPort,
    RoleConfig,
    ToolCallRequest,
    digest_of,
    put_json_artifact,
    record_mcp_request,
    record_mcp_response,
)
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import AgentTaskOutput, HitlPolicy
from coscientist.core.execution.tool_registry import (
    RegisteredToolSpec,
    _register_tool_for_tests,
    _reset_registry_for_tests,
)
from coscientist.llm.types import LlmRequest, LlmResponse, Usage
from coscientist.tools.mcp_adapter import McpJournalHooks, tools_from_mcp_session
from tests.helpers import close_runtime, open_runtime
from tests.mcp.conftest import make_client, make_config
from tests.mcp.fake_server import FakeStdioServer, default_handler


class _In(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    query: str


class _Mono:
    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        self._t += 0.01
        return self._t


class FakeLlm:
    complete_timeout_s = 1.0

    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)
        self.calls = 0

    async def complete(self, request: LlmRequest, *, timeout_s: float) -> LlmResponse:
        self.calls += 1
        return LlmResponse(
            content=self._contents.pop(0),
            model=request.model,
            finish_reason="stop",
            usage=Usage(prompt_tokens=4, completion_tokens=6, latency_ms=2.0),
        )


class RecordingFetcher:
    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("file fetch not expected in this e2e")


@pytest.fixture(autouse=True)
def _registry():
    _reset_registry_for_tests()
    _register_tool_for_tests(
        RegisteredToolSpec(
            name="echo",
            effect="read",
            provider="mcp",
            is_fetch=False,
            idempotent=True,
        )
    )
    yield
    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_fake_llm_and_mcp_journal_events(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_agent_mcp")
    await rt.create_study(
        profile="research",
        request="mcp e2e",
        catalog_version=3,
        sid="st_agent_mcp",
        finalize_reserve_s=5,
    )
    arts = LocalArtifactStore(tmp_path / "cas")
    mono = _Mono()
    hooks = McpJournalHooks(
        record_request=record_mcp_request,
        record_response=record_mcp_response,
        put_json_artifact=put_json_artifact,
        digest_of=digest_of,
    )

    turn1 = AgentDecision(
        tool_calls=(ToolCallRequest(name="echo", arguments={"text": "ping"}),),
        final=None,
    ).model_dump_json()
    turn2 = AgentDecision(
        tool_calls=(),
        final=AgentTaskOutput(summary="mcp-ok", values={"echoed": "ping"}),
    ).model_dump_json()
    fake = FakeLlm([turn1, turn2])

    async with FakeStdioServer(default_handler) as server:
        client = make_client(make_config(), file_fetcher=RecordingFetcher())
        session = await client.connect_memory("fake", server.client)
        mcp_tools = await tools_from_mcp_session(
            session,
            server_name="fake",
            clock=rt.clock,
            monotonic=mono,
            call_tool_timeout_s=2.0,
            effect="read",
            is_fetch=False,
            idempotent=True,
            writer=rt.writer,
            artifacts=arts,
            journal=hooks,
            on_event=rt._project_local,
        )
        assert [t.spec.name for t in mcp_tools] == ["echo"]

        port = JournaledAgentPort(
            config=AgentConfig(
                roles={
                    "executor": RoleConfig(
                        model="fake-model",
                        system_prompt_ref="sys",
                        max_turns=4,
                        allowed_tool_names=("echo",),
                    )
                },
                prompts={"sys": "Use echo then finish."},
                cost_per_prompt_token_rub=None,
                cost_per_completion_token_rub=None,
            ),
            llm=fake,
            writer=rt.writer,
            artifacts=arts,
            clock=rt.clock,
            study_id="st_agent_mcp",
            attempt_id="att#1",
            task_key="EXP-0",
            hitl_policy=HitlPolicy(),
            on_event=rt._project_local,
            monotonic=mono,
        )
        try:
            run = await port.run(
                "executor",
                _In(query="go"),
                tools=list(mcp_tools),
                budget=AgentBudgetLimits(
                    max_llm_calls=5,
                    max_tokens=10_000,
                    max_tool_calls=5,
                    max_cost_rub=None,
                ),
            )
            assert run.status == "ok"
            assert run.output is not None
            assert run.output.summary == "mcp-ok"
            assert fake.calls == 2
            assert len(run.tool_calls) == 1

            events = await rt.store.read_all()
            types = {e.type for e in events}
            assert "LlmCallRecorded" in types
            assert "UsageRecorded" in types
            assert "ToolCalled" in types
            assert "McpRequestRecorded" in types
            assert "McpResponseRecorded" in types

            mcp_req = next(e for e in events if e.type == "McpRequestRecorded")
            assert mcp_req.payload["server_name"] == "fake"
            assert mcp_req.payload["tool_name"] == "echo"
            assert mcp_req.payload["method"] == "tools/call"
            mcp_resp = next(e for e in events if e.type == "McpResponseRecorded")
            assert mcp_resp.payload["content_digest"]
            assert mcp_resp.payload["is_error"] is False
            tool_ev = next(e for e in events if e.type == "ToolCalled")
            assert tool_ev.payload["provider"] == "mcp"
            assert tool_ev.payload["ok"] is True
        finally:
            await session.aclose()
            await close_runtime(rt)
