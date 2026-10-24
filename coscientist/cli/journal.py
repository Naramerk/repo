"""Map agent runs → ``AgentTaskOutput`` and journal via ``Executor.record``."""

from __future__ import annotations

import json
from typing import Any, Protocol

from coscientist.agents.research_spec import ResearchSpec
from coscientist.agents.schemas import AgentRun, ExecutorRouteOut, JsonEntry
from coscientist.cli.errors import CliRefuse
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle, Task


class _ExecutorPort(Protocol):
    artifacts: Any

    async def record(self, aid: str, output: AgentTaskOutput) -> None: ...


class _RuntimePort(Protocol):
    executor: _ExecutorPort | None
    attempts: dict[str, Any]
    tasks: dict[str, Task]


def _parse_values(entries: tuple[JsonEntry, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for entry in entries:
        try:
            out[entry.key] = json.loads(entry.value_json)
        except json.JSONDecodeError as exc:
            raise CliRefuse(
                f"ExecutorRouteOut.values[{entry.key!r}] value_json is not JSON: {exc}",
                code=4,
            ) from exc
    return out


async def record_research_spec(rt: _RuntimePort, aid: str, spec: ResearchSpec) -> None:
    """Journal SPEC SUCCESS from ``tz_spec`` output (artifact_present default)."""
    if rt.executor is None:
        raise CliRefuse("runtime has no executor", code=4)
    content = spec.model_dump_json().encode("utf-8")
    art = rt.executor.artifacts.put(
        content,
        make_tool_ref(f"{aid}:tz_spec"),
        name="main",
        media_type="application/json",
    )
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="tz_spec",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values=spec.model_dump(mode="json"),
        ),
    )


async def record_executor_route(rt: _RuntimePort, aid: str, run: AgentRun) -> None:
    """Map ``ExecutorRouteOut`` + tool results → ``AgentTaskOutput`` and record.

    Claims are not invented: ``ExecutorRouteOut`` has no claims field → ``claims=[]``.
    Expected output names come from the journaled task spec (refuse if attempt/task missing).
    """
    if rt.executor is None:
        raise CliRefuse("runtime has no executor", code=4)
    if run.error is not None:
        raise CliRefuse(f"executor-route error: {run.error}", code=4)
    out = run.output
    if not isinstance(out, ExecutorRouteOut):
        raise CliRefuse(
            f"executor-route must return ExecutorRouteOut, got {type(out).__name__}",
            code=4,
        )
    attempt = rt.attempts.get(aid)
    if not isinstance(attempt, dict):
        raise CliRefuse(f"attempt {aid!r} missing from runtime.attempts", code=4)
    task_id = attempt.get("task_id")
    if type(task_id) is not str or not task_id:
        raise CliRefuse(f"attempt {aid!r} has no task_id", code=4)
    task = rt.tasks.get(task_id)
    if task is None:
        raise CliRefuse(f"task {task_id!r} missing from runtime.tasks", code=4)

    values = _parse_values(out.values)
    outputs: dict[str, ArtifactHandle] = {}
    for ref in run.tool_calls:
        if ref.result_json is None:
            raise CliRefuse(f"tool {ref.tool_name!r} missing result_json", code=4)
        body = ref.result_json.encode("utf-8")
        art = rt.executor.artifacts.put(
            body,
            make_tool_ref(f"{aid}:{ref.tool_name}"),
            name=ref.tool_name,
            media_type="application/json",
        )
        outputs[ref.tool_name] = ArtifactHandle(artifact_id=art.artifact_id, name=ref.tool_name)

    expected = list(task.spec.expected_outputs)
    for o in expected:
        if o.name in outputs:
            continue
        if len(run.tool_calls) == 1:
            sole = run.tool_calls[0]
            if sole.result_json is None:
                raise CliRefuse(f"tool {sole.tool_name!r} missing result_json", code=4)
            body = sole.result_json.encode("utf-8")
            art = rt.executor.artifacts.put(
                body,
                make_tool_ref(f"{aid}:{o.name}"),
                name=o.name,
                media_type=o.media_type,
            )
            outputs[o.name] = ArtifactHandle(artifact_id=art.artifact_id, name=o.name)
            continue
        if len(run.tool_calls) == 0:
            body = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            art = rt.executor.artifacts.put(
                body,
                make_tool_ref(f"{aid}:{o.name}"),
                name=o.name,
                media_type=o.media_type,
            )
            outputs[o.name] = ArtifactHandle(artifact_id=art.artifact_id, name=o.name)
            continue
        raise CliRefuse(
            f"expected output {o.name!r} not produced: multiple tools "
            f"{[c.tool_name for c in run.tool_calls]!r} and no matching name",
            code=4,
        )

    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary=out.summary,
            outputs=outputs,
            values=values,
            claims=[],
        ),
    )
