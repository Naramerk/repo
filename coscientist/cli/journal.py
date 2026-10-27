"""Map agent runs → ``AgentTaskOutput`` and journal via ``Executor.record``."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Protocol

from coscientist.agents.research_spec import ResearchSpec
from coscientist.agents.schemas import AgentRun, ExecutorRouteOut, HypothesisOut, LiteratureOut, ToolArg
from coscientist.agents.schemas import ToolCallRef as AgentToolCallRef
from coscientist.cli.errors import CliRefuse
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    Artifact,
    ArtifactHandle,
    ArtifactProvenance,
    Task,
    ToolCallRef,
)


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
        provenance: ArtifactProvenance = None,
    ) -> _PutArtifact: ...

    def get_meta(self, artifact_id: str) -> Artifact | None: ...


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


def _receipt_handle(executor: _ExecutorPort, aid: str, ref: AgentToolCallRef) -> ArtifactHandle:
    if ref.result_json is None:
        raise CliRefuse(f"tool {ref.tool_name!r} missing result_json", code=4)
    body = ref.result_json.encode("utf-8")
    if ref.artifact_id is not None:
        meta = executor.artifacts.get_meta(ref.artifact_id)
        if (
            meta is None
            or meta.sha256 != hashlib.sha256(body).hexdigest()
            or meta.provenance != "tool"
            or meta.produced_by.call_id != ref.call_id
            or meta.produced_by.tool != ref.tool_name
        ):
            raise CliRefuse("tool receipt artifact binding mismatch", code=4)
        return ArtifactHandle(artifact_id=meta.artifact_id, name=ref.call_id or ref.tool_name)
    art = executor.artifacts.put(
        body,
        make_tool_ref(f"{aid}:{ref.tool_name}", tool=ref.tool_name),
        name=ref.tool_name,
        media_type="application/json",
        provenance="tool",
    )
    return ArtifactHandle(artifact_id=_artifact_id(art), name=ref.tool_name)


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
    Provenance is stamped from the attempt's recorded ``tool_calls`` (never model
    claims): tool_call results → ``provenance="tool"`` (+ tool name/call ref on
    ``produced_by``); ``values`` echoes → ``provenance="agent"`` / ``producer="agent"``.
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
    if run.output_selections is not None:
        selected_names = [selection.output_name for selection in run.output_selections]
        required_names = {spec.name for spec in task.spec.expected_outputs if spec.required}
        duplicate_names = len(set(selected_names)) != len(selected_names)
        missing_names = required_names - set(selected_names)
        if duplicate_names or missing_names:
            raise CliRefuse("automatic finish must select every frozen required output exactly once", code=4)
    outputs: dict[str, ArtifactHandle] = {}
    receipts: dict[str, ArtifactHandle] = {}
    if run.output_selections is None and any(ref.outcome != "ok" for ref in run.tool_calls):
        raise CliRefuse("fixed recipe contains a failed tool receipt", code=4)
    for ref in run.tool_calls:
        handle = _receipt_handle(rt.executor, aid, ref)
        receipt_key = ref.call_id or ref.tool_name
        if receipt_key in receipts:
            raise CliRefuse("duplicate tool receipt identity", code=4)
        receipts[receipt_key] = handle
        outputs[receipt_key] = handle
        if run.output_selections is None and ref.outcome == "ok":
            outputs[ref.tool_name] = handle

    if run.output_selections is not None:
        for selection in run.output_selections:
            matching = [ref for ref in run.tool_calls if ref.call_id == selection.call_id and ref.outcome == "ok"]
            permitted = {spec.name for spec in task.spec.expected_outputs} | {ref.tool_name for ref in run.tool_calls}
            if selection.call_id not in receipts or len(matching) != 1 or selection.output_name not in permitted:
                raise CliRefuse("output selection cites unknown receipt", code=4)
            outputs[selection.output_name] = receipts[selection.call_id]

    for o in task.spec.expected_outputs:
        if o.name in outputs:
            continue
        if o.name not in values:
            if not o.required:
                continue
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
            # Model-authored bytes: visible producer=agent (never tool-backed).
            provenance="agent",
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


def _wired_stage_ctx(rt: _RuntimePort, aid: str) -> tuple[_ExecutorPort, Task]:
    """Runtime-drift checks shared by the wired HYP/LIT record path (SPEC-mirror)."""
    executor = rt.executor
    if executor is None:
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
    return executor, task


def _expected_output_artifacts(
    executor: _ExecutorPort,
    aid: str,
    task: Task,
    content: bytes,
    *,
    role: str,
) -> dict[str, ArtifactHandle]:
    """Artifact per journaled task ``expected_outputs`` name (no invented names)."""
    outputs: dict[str, ArtifactHandle] = {}
    for o in task.spec.expected_outputs:
        art = executor.artifacts.put(
            content,
            make_tool_ref(f"{aid}:{role}:{o.name}"),
            name=o.name,
            media_type=o.media_type,
        )
        outputs[o.name] = ArtifactHandle(artifact_id=_artifact_id(art), name=o.name)
    return outputs


async def record_hypotheses(rt: _RuntimePort, aid: str, out: HypothesisOut) -> None:
    """Journal HYP SUCCESS from ``hypothesizer`` output (mirrors SPEC record).

    Output artifact names come only from the journaled HYP task's
    ``expected_outputs`` (the intake stage's declared artifact_present output)
    — the same PI1b existence gate closes the task as SPEC.
    """
    executor, task = _wired_stage_ctx(rt, aid)
    await executor.record(
        aid,
        AgentTaskOutput(
            summary="hypothesizer",
            outputs=_expected_output_artifacts(
                executor, aid, task, out.model_dump_json().encode("utf-8"), role="hypothesizer"
            ),
            values=out.model_dump(mode="json"),
        ),
    )


async def record_literature(rt: _RuntimePort, aid: str, run: AgentRun) -> None:
    """Journal LIT SUCCESS from ``researcher`` run (tool-backed, no invented papers).

    Tool results become ``provenance="tool"`` artifacts exactly like
    ``record_executor_route``. Every ``LiteratureOut.paper`` must cite the exact
    journaled tool_call name AND its ``identity_key`` must appear verbatim in
    that tool's result JSON — papers only from tool outputs (fail-closed).
    """
    if not isinstance(run.output, LiteratureOut):
        raise CliRefuse(
            f"researcher must return LiteratureOut, got {type(run.output).__name__}",
            code=4,
        )
    out = run.output
    executor, task = _wired_stage_ctx(rt, aid)

    outputs: dict[str, ArtifactHandle] = {}
    result_by_tool: dict[str, list[str]] = {}
    for ref in run.tool_calls:
        handle = _receipt_handle(executor, aid, ref)
        key = ref.call_id or ref.tool_name
        if key in outputs:
            raise CliRefuse("duplicate literature receipt identity", code=4)
        outputs[key] = handle
        if ref.outcome == "ok":
            assert ref.result_json is not None
            if ref.tool_name in result_by_tool:
                result_by_tool[ref.tool_name].append(ref.result_json)
            else:
                result_by_tool[ref.tool_name] = [ref.result_json]

    for i, paper in enumerate(out.papers):
        result_json = result_by_tool.get(paper.source_tool)
        if result_json is None:
            raise CliRefuse(
                f"paper[{i}] cites {paper.source_tool!r} but no journaled tool result "
                f"carries that name (tools={sorted(result_by_tool)!r}) — no invented citations",
                code=4,
            )
        if not any(paper.identity_key in body for body in result_json):
            raise CliRefuse(
                f"paper[{i}] identity_key not found in the {paper.source_tool!r} result — "
                "papers only from tool outputs (no invented citations)",
                code=4,
            )

    await executor.record(
        aid,
        AgentTaskOutput(
            summary="researcher",
            outputs={
                **outputs,
                **_expected_output_artifacts(
                    executor, aid, task, out.model_dump_json().encode("utf-8"), role="researcher"
                ),
            },
            values=out.model_dump(mode="json"),
            claims=[],
        ),
    )
