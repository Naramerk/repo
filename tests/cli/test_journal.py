"""CLI journal mappers — AgentRun → Executor.record; typed refuse on bad payloads."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coscientist.agents.research_spec import Constraint, Goal, ResearchSpec, SuccessMetric
from coscientist.agents.schemas import AgentRun, ExecutorRouteOut, JsonEntry, ToolCallRef, Usage
from coscientist.cli.errors import CliRefuse
from coscientist.cli.journal import (
    record_executor_route,
    record_research_spec,
    record_stage_pass,
)
from coscientist.core.execution.domain import AgentTaskOutput


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
        self.executor = _FakeExecutor() if with_executor else None
        self.attempts: dict[str, dict[str, str]] = {"a1": {"task_id": "t1"}}
        self.tasks: dict[str, Any] = {
            "t1": SimpleNamespace(spec=SimpleNamespace(addresses=["Q1"]))
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
        success_metrics=(
            SuccessMetric(metric="m", op="==", target="1", check="code", tolerance=None),
        ),
    )
    await record_research_spec(rt, "a1", spec)  # type: ignore[arg-type]
    assert rt.executor is not None
    assert len(rt.executor.records) == 1
    aid, out = rt.executor.records[0]
    assert aid == "a1"
    assert "main" in out.outputs
    assert out.values["object"] == "o"


@pytest.mark.asyncio
async def test_record_stage_pass_journals_values() -> None:
    rt = _FakeRuntime()
    await record_stage_pass(rt, "a1", stage="HYP")  # type: ignore[arg-type]
    assert rt.executor is not None
    _aid, out = rt.executor.records[0]
    assert out.values == {"stage": "HYP", "status": "pass", "agent": None}


@pytest.mark.asyncio
async def test_record_executor_route_maps_tool_and_claims() -> None:
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
    await record_executor_route(rt, "a1", run)  # type: ignore[arg-type]
    assert rt.executor is not None
    _aid, out = rt.executor.records[0]
    assert "main" in out.outputs
    assert "dataset_overview" in out.outputs
    assert out.claims and out.claims[0]["target"] == "Q1"


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
        await record_executor_route(rt, "a1", run)  # type: ignore[arg-type]
    assert ei.value.code == 4


@pytest.mark.asyncio
async def test_record_without_executor_refuses() -> None:
    rt = _FakeRuntime(with_executor=False)
    with pytest.raises(CliRefuse, match="no executor") as ei:
        await record_stage_pass(rt, "a1", stage="LIT")  # type: ignore[arg-type]
    assert ei.value.code == 4
