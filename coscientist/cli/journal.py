"""Map agent runs → ``AgentTaskOutput`` and journal via ``Executor.record``."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from coscientist.agents.research_spec import ResearchSpec
from coscientist.agents.schemas import AgentRun, ExecutorRouteOut, ToolArg
from coscientist.cli.errors import CliRefuse
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle, Task, ToolCallRef


class _PutArtifact(Protocol):
    @property
    def artifact_id(self) -> str: ...


class _ArtifactStore(Protocol):
    # Param types must match LocalArtifactStore.put (contravariance): produced_by is ToolCallRef.
    def put(
        self,
        content: bytes,
        produced_by: ToolCallRef,
        *,
        name: str,
        media_type: str,
    ) -> _PutArtifact: ...


class _ExecutorPort(Protocol):
    # Property: LocalArtifactStore / Executor attribute types are invariant as vars.
    @property
    def artifacts(self) -> _ArtifactStore: ...

    # Executor.record returns Result; Protocol return must accept that (not None).
    async def record(self, aid: str, output: AgentTaskOutput) -> object: ...


class _RuntimePort(Protocol):
    # Read-only properties so StudyRuntime's concrete attributes satisfy the Protocol
    # (mutable attribute types are invariant under mypy structural checks; main typed
    # MachineRuntime.executor as Executor | None).
    @property
    def executor(self) -> _ExecutorPort | None: ...

    @property
    def attempts(self) -> Mapping[str, object]: ...

    @property
    def tasks(self) -> Mapping[str, Task]: ...


def _parse_values(entries: tuple[ToolArg, ...]) -> dict[str, object]:
    out: dict[str, object] = {}
    for entry in entries:
        try:
            parsed: object = json.loads(entry.value_json)
        except json.JSONDecodeError as exc:
            raise CliRefuse(
                f"ExecutorRouteOut.values[{entry.key!r}] value_json is not JSON: {exc}",
                code=4,
            ) from exc
        out[entry.key] = parsed
    return out


def _artifact_id(art: _PutArtifact) -> str:
    aid = art.artifact_id
    if type(aid) is not str or not aid:
        raise CliRefuse("artifacts.put returned object without artifact_id str", code=4)
    return aid


async def record_research_spec(rt: _RuntimePort, aid: str, spec: ResearchSpec) -> None:
    """Journal SPEC SUCCESS from ``tz_spec`` output.

    Output artifact names come only from the journaled SPEC task's ``expected_outputs``
    (fail-closed: no invented ``\"main\"``). Empty expected_outputs → values only.
    """
    if rt.executor is None:
        raise CliRefuse("runtime has no executor", code=4)
    attempt = rt.attempts.get(aid)
    if not isinstance(attempt, Mapping):
        raise CliRefuse(f"attempt {aid!r} missing from runtime.attempts", code=4)
    task_id = attempt.get("task_id")
    if type(task_id) is not str or not task_id:
        raise CliRefuse(f"attempt {aid!r} has no task_id", code=4)
    task = rt.tasks.get(task_id)
    if task is None:
        raise CliRefuse(f"task {task_id!r} missing from runtime.tasks", code=4)

    content = spec.model_dump_json().encode("utf-8")
    outputs: dict[str, ArtifactHandle] = {}
    for o in task.spec.expected_outputs:
        art = rt.executor.artifacts.put(
            content,
            make_tool_ref(f"{aid}:tz_spec:{o.name}"),
            name=o.name,
            media_type=o.media_type,
        )
        outputs[o.name] = ArtifactHandle(artifact_id=_artifact_id(art), name=o.name)
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="tz_spec",
            outputs=outputs,
            values=spec.model_dump(mode="json"),
        ),
    )


async def record_executor_route(rt: _RuntimePort, aid: str, run: AgentRun) -> None:
    """Map ``ExecutorRouteOut`` + tool results → ``AgentTaskOutput`` and record.

    Claims are not invented: ``ExecutorRouteOut`` has no claims field → ``claims=[]``.
    Expected output names must match a tool_call name or a ``values`` key — no sole-tool
    / dump-values guessing (fail-closed).
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
    if not isinstance(attempt, Mapping):
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
        outputs[ref.tool_name] = ArtifactHandle(
            artifact_id=_artifact_id(art),
            name=ref.tool_name,
        )

    for o in task.spec.expected_outputs:
        if o.name in outputs:
            continue
        if o.name not in values:
            raise CliRefuse(
                f"expected output {o.name!r} not produced: no tool_call named {o.name!r} "
                f"and no ExecutorRouteOut.values[{o.name!r}] "
                f"(tools={[c.tool_name for c in run.tool_calls]!r})",
                code=4,
            )
        body = json.dumps(values[o.name], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        art = rt.executor.artifacts.put(
            body,
            make_tool_ref(f"{aid}:{o.name}"),
            name=o.name,
            media_type=o.media_type,
        )
        outputs[o.name] = ArtifactHandle(artifact_id=_artifact_id(art), name=o.name)

    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary=out.summary,
            outputs=outputs,
            values=values,
            claims=[],
        ),
    )
