"""CLI journal mappers — AgentRun → Executor.record; typed refuse on bad payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from coscientist.agents.research_spec import Constraint, Goal, ResearchSpec, SuccessMetric
from coscientist.agents.schemas import AgentRun, ExecutorRouteOut, JsonEntry, ToolCallRef
from coscientist.cli.errors import CliRefuse
from coscientist.cli.journal import record_executor_route, record_research_spec
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    OutputSpec,
    Task,
    TaskKind,
    TaskSpec,
    TaskState,
)
from coscientist.llm.types import Usage


class _FakeArtifacts:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put(self, content: bytes, ref: Any, *, name: str, media_type: str) -> Any:
        del ref
        aid = f"art_{len(self.puts)}"
        self.puts.append({"name": name, "media_type": media_type, "nbytes": len(content)})
        return SimpleNamespace(artifact_id=aid)


class _FakeExecutor:
    def __init__(self) -> None:
        self.artifacts = _FakeArtifacts()
        self.records: list[tuple[str, AgentTaskOutput]] = []

    async def record(self, aid: str, output: AgentTaskOutput) -> None:
        self.records.append((aid, output))


class _FakeRuntime:
    def __init__(self, *, with_executor: bool = True) -> None:
        self.executor: _FakeExecutor | None = _FakeExecutor() if with_executor else None
        self.attempts: dict[str, dict[str, str]] = {"a1": {"task_id": "t1"}}
        self.tasks: dict[str, Task] = {
            "t1": Task(
                task_id="t1",
                kind=TaskKind.EXPERIMENT,
                spec=TaskSpec(
                    task_key="EXP-0",
                    title="t",
                    goal="g",
                    route="experiment",
                    expected_outputs=[
                        OutputSpec(
                            name="main",
                            media_type="application/json",
                            schema_ref=None,
                            required=True,
                        )
                    ],
                    addresses=["Q1"],
                ),
                state=TaskState.RUNNING,
                deadline=datetime(2026, 1, 2, tzinfo=UTC),
            )
        }


def _usage() -> Usage:
    return Usage(prompt_tokens=1, completion_tokens=1, latency_ms=1.0)


@pytest.mark.asyncio
async def test_record_research_spec_journals_main_artifact() -> None:
    rt = _FakeRuntime()
    spec = ResearchSpec(
        object="o",
        goal=Goal(statement="g", research_form=None, trl=None),
        scope="s",
        constraints=(Constraint(subtype="resource", content="c", limit=None),),
        success_metrics=(SuccessMetric(metric="m", op="==", target="1", check="code", tolerance=None),),
    )
    await record_research_spec(rt, "a1", spec)
    assert rt.executor is not None
    assert len(rt.executor.records) == 1
    aid, out = rt.executor.records[0]
    assert aid == "a1"
    assert "main" in out.outputs
    assert out.values["object"] == "o"


@pytest.mark.asyncio
async def test_record_executor_route_maps_tool_to_expected_main() -> None:
    rt = _FakeRuntime()
    run = AgentRun(
        output=ExecutorRouteOut(
            summary="ok",
            values=(JsonEntry(key="ok", value_json="true"),),
            tool_found=("dataset_overview",),
        ),
        usage=_usage(),
        tool_calls=(
            ToolCallRef(
                tool_name="dataset_overview",
                resolution_status="tool_found",
                args=(),
                result_json='{"ok":true}',
            ),
        ),
        error=None,
    )
    await record_executor_route(rt, "a1", run)
    assert rt.executor is not None
    _aid, out = rt.executor.records[0]
    assert "main" in out.outputs
    assert "dataset_overview" in out.outputs
    assert out.claims == []


@pytest.mark.asyncio
async def test_record_executor_route_bad_json_refuses() -> None:
    rt = _FakeRuntime()
    run = AgentRun(
        output=ExecutorRouteOut(
            summary="ok",
            values=(JsonEntry(key="bad", value_json="not-json"),),
            tool_found=(),
        ),
        usage=_usage(),
        tool_calls=(),
        error=None,
    )
    with pytest.raises(CliRefuse, match="value_json is not JSON") as ei:
        await record_executor_route(rt, "a1", run)
    assert ei.value.code == 4


@pytest.mark.asyncio
async def test_record_without_executor_refuses() -> None:
    rt = _FakeRuntime(with_executor=False)
    spec = ResearchSpec(
        object="o",
        goal=Goal(statement="g", research_form=None, trl=None),
        scope="s",
        constraints=(Constraint(subtype="resource", content="c", limit=None),),
        success_metrics=(SuccessMetric(metric="m", op="==", target="1", check="code", tolerance=None),),
    )
    with pytest.raises(CliRefuse, match="no executor") as ei:
        await record_research_spec(rt, "a1", spec)
    assert ei.value.code == 4


@pytest.mark.asyncio
async def test_record_executor_missing_attempt_refuses() -> None:
    rt = _FakeRuntime()
    rt.attempts = {}
    run = AgentRun(
        output=ExecutorRouteOut(
            summary="ok",
            values=(),
            tool_found=(),
        ),
        usage=_usage(),
        tool_calls=(),
        error=None,
    )
    with pytest.raises(CliRefuse, match="missing from runtime.attempts") as ei:
        await record_executor_route(rt, "a1", run)
    assert ei.value.code == 4
