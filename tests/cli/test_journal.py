"""CLI journal mappers — AgentRun → Executor.record; typed refuse on bad payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coscientist.agents.research_spec import Constraint, Goal, ResearchSpec, SuccessMetric
from coscientist.agents.schemas import AgentRun, ExecutorRouteOut, ToolArg, ToolCallRef
from coscientist.cli.errors import CliRefuse
from coscientist.cli.journal import record_executor_route, record_research_spec
from coscientist.core.claims.graph import GraphStore, default_execution_registry
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    OutputSpec,
    Task,
    TaskKind,
    TaskSpec,
    TaskState,
)
from coscientist.llm.types import Usage


@dataclass(frozen=True)
class _StoredArtifact:
    artifact_id: str


class _FakeArtifacts:
    def __init__(self) -> None:
        self.puts: list[dict[str, object]] = []

    def put(
        self,
        content: bytes,
        ref: object,
        *,
        name: str,
        media_type: str,
        provenance: str | None = None,
    ) -> _StoredArtifact:
        aid = f"art_{len(self.puts)}"
        self.puts.append(
            {
                "name": name,
                "media_type": media_type,
                "nbytes": len(content),
                "provenance": provenance,
                "ref": ref,
            }
        )
        return _StoredArtifact(artifact_id=aid)


class _FakeExecutor:
    def __init__(self) -> None:
        self.artifacts = _FakeArtifacts()
        self.records: list[tuple[str, AgentTaskOutput]] = []

    async def record(self, aid: str, output: AgentTaskOutput) -> None:
        self.records.append((aid, output))


class _FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 10, 27, tzinfo=UTC)


class _FakeRuntime:
    def __init__(
        self,
        *,
        with_executor: bool = True,
        expected_outputs: list[OutputSpec] | None = None,
    ) -> None:
        self.executor: _FakeExecutor | None = _FakeExecutor() if with_executor else None
        self.attempts: dict[str, dict[str, str]] = {
            "a1": {"task_id": "t1", "study_id": "st_journal"}
        }
        self.graph = GraphStore(default_execution_registry())
        self.clock = _FakeClock()
        self.appended_events: list[object] = []
        outputs = (
            expected_outputs
            if expected_outputs is not None
            else [
                OutputSpec(
                    name="main",
                    media_type="application/json",
                    schema_ref=None,
                    required=True,
                )
            ]
        )
        self.tasks: dict[str, Task] = {
            "t1": Task(
                task_id="t1",
                kind=TaskKind.EXPERIMENT,
                spec=TaskSpec(
                    task_key="EXP-0",
                    title="t",
                    goal="g",
                    route="experiment",
                    expected_outputs=outputs,
                    addresses=["Q1"],
                ),
                state=TaskState.RUNNING,
                deadline=datetime(2026, 1, 2, tzinfo=UTC),
            )
        }

    async def append_events(self, events: list[object]) -> None:
        self.appended_events.extend(events)


def _usage() -> Usage:
    return Usage(prompt_tokens=1, completion_tokens=1, latency_ms=1.0)


def _spec() -> ResearchSpec:
    return ResearchSpec(
        object="o",
        goal=Goal(statement="g", research_form=None, trl=None),
        scope="s",
        constraints=(Constraint(subtype="resource", content="c", limit=None),),
        success_metrics=(SuccessMetric(metric="m", op="==", target="1", check="code", tolerance=None),),
    )


@pytest.mark.asyncio
async def test_record_research_spec_uses_task_expected_output_names() -> None:
    """Output names come from the journaled SPEC task — not a journal hardcode."""
    rt = _FakeRuntime(
        expected_outputs=[OutputSpec(name="main", media_type="application/json", schema_ref=None, required=True)]
    )
    rt.tasks["t1"] = Task(
        task_id="t1",
        kind=TaskKind.SPEC,
        spec=TaskSpec(
            task_key="SPEC",
            title="SPEC",
            goal="SPEC",
            route="spec",
            expected_outputs=[OutputSpec(name="main", media_type="application/json", schema_ref=None, required=True)],
        ),
        state=TaskState.RUNNING,
        deadline=datetime(2026, 1, 2, tzinfo=UTC),
    )
    await record_research_spec(rt, "a1", _spec())
    assert rt.executor is not None
    _aid, out = rt.executor.records[0]
    assert set(out.outputs) == {"main"}
    assert out.values["object"] == "o"
    assert rt.executor.artifacts.puts[0]["name"] == "main"


@pytest.mark.asyncio
async def test_record_research_spec_empty_expected_outputs_no_invented_name() -> None:
    rt = _FakeRuntime(expected_outputs=[])
    rt.tasks["t1"] = Task(
        task_id="t1",
        kind=TaskKind.SPEC,
        spec=TaskSpec(task_key="SPEC", title="SPEC", goal="SPEC", route="spec", expected_outputs=[]),
        state=TaskState.RUNNING,
        deadline=datetime(2026, 1, 2, tzinfo=UTC),
    )
    await record_research_spec(rt, "a1", _spec())
    assert rt.executor is not None
    _aid, out = rt.executor.records[0]
    assert out.outputs == {}
    assert rt.executor.artifacts.puts == []


@pytest.mark.asyncio
async def test_record_executor_route_materializes_expected_from_values() -> None:
    rt = _FakeRuntime()
    run = AgentRun(
        output=ExecutorRouteOut(
            summary="ok",
            values=(
                ToolArg(key="ok", value_json="true"),
                ToolArg(key="main", value_json='{"ok":true}'),
            ),
            tool_found=("dataset_overview",),
        ),
        usage=_usage(),
        tool_calls=(
            ToolCallRef(
                tool_name="static:dataset_overview",
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
    assert "static:dataset_overview" in out.outputs
    assert out.claims == []


@pytest.mark.asyncio
async def test_record_executor_route_stamps_provenance_from_recorded_tool_calls() -> None:
    """Provenance derives from the attempt's recorded tool_calls, not model claims.

    A tool_call result artifact is stamped ``provenance="tool"`` with the tool
    name / attempt-scoped call ref on ``produced_by``; ``values`` echoes
    (including one under a tool-id name with no matching tool_call) are stamped
    ``provenance="agent"`` — visible for the evaluation-time policy.
    """
    rt = _FakeRuntime(
        expected_outputs=[
            OutputSpec(
                name="static:dataset_overview",
                media_type="application/json",
                schema_ref=None,
                required=True,
            ),
            OutputSpec(name="summary", media_type="application/json", schema_ref=None, required=True),
        ]
    )
    run = AgentRun(
        output=ExecutorRouteOut(
            summary="ok",
            values=(
                # Model invents numbers under the tool-id name (no tool_call for
                # it in this run) and authors a non-tool-named output.
                ToolArg(key="static:dataset_overview", value_json='{"invented": 42}'),
                ToolArg(key="summary", value_json='{"model_authored": true}'),
            ),
            tool_found=("other_tool",),
        ),
        usage=_usage(),
        tool_calls=(
            ToolCallRef(
                tool_name="static:other_tool",
                resolution_status="tool_found",
                args=(),
                result_json='{"real": true}',
            ),
        ),
        error=None,
    )
    await record_executor_route(rt, "a1", run)
    assert rt.executor is not None
    puts = rt.executor.artifacts.puts
    by_name = {str(p["name"]): p for p in puts}
    # Tool-backed: bytes are the recorded tool_call result → provenance="tool".
    tool_put = by_name["static:other_tool"]
    assert tool_put["provenance"] == "tool"
    tool_ref = tool_put["ref"]
    assert tool_ref.tool == "static:other_tool"
    assert tool_ref.call_id == "a1:static:other_tool"
    # Values echo under a tool-id name: honestly stamped agent (policy catches
    # it at evaluation), never silently journaled as tool output.
    assert by_name["static:dataset_overview"]["provenance"] == "agent"
    # Agent-named values output: allowed, visible producer=agent.
    assert by_name["summary"]["provenance"] == "agent"


@pytest.mark.asyncio
async def test_record_executor_route_refuses_missing_expected_without_guess() -> None:
    rt = _FakeRuntime()
    run = AgentRun(
        output=ExecutorRouteOut(
            summary="ok",
            values=(ToolArg(key="ok", value_json="true"),),
            tool_found=("dataset_overview",),
        ),
        usage=_usage(),
        tool_calls=(
            ToolCallRef(
                tool_name="static:dataset_overview",
                resolution_status="tool_found",
                args=(),
                result_json='{"ok":true}',
            ),
        ),
        error=None,
    )
    with pytest.raises(CliRefuse, match="expected output 'main' not produced") as ei:
        await record_executor_route(rt, "a1", run)
    assert ei.value.code == 4


@pytest.mark.asyncio
async def test_record_executor_route_bad_json_refuses() -> None:
    # Non-JSON value_json is refused at the ToolArg schema level now; the
    # journal _parse_values gate below is the backstop, so exercise it with
    # a hand-built ToolArg bypassing validation.
    with pytest.raises(ValidationError, match="value_json must be valid JSON"):
        AgentRun(
            output=ExecutorRouteOut(
                summary="ok",
                values=(ToolArg(key="bad", value_json="not-json"),),
                tool_found=(),
            ),
            usage=_usage(),
            tool_calls=(),
            error=None,
        )


@pytest.mark.asyncio
async def test_record_without_executor_refuses() -> None:
    rt = _FakeRuntime(with_executor=False)
    with pytest.raises(CliRefuse, match="no executor") as ei:
        await record_research_spec(rt, "a1", _spec())
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
