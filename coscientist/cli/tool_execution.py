"""Runtime-owned MCP dispatch through the existing effect and journal boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Literal

from jsonschema import validators
from referencing import Registry

from coscientist.agents.schemas import AgentError, ToolArg, ToolCallRef
from coscientist.core.events.digest import canonical_json
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.execution.call_ctx import CallCtx
from coscientist.core.execution.domain import ToolCallRef as RecordedToolCallRef
from coscientist.core.execution.machine import StudyRuntime
from coscientist.tools.contract import ToolError
from coscientist.tools.effects import invoke_tool
from coscientist.tools.router import ToolNeed, ToolRouter


def validate_tool_arguments(schema: dict, args: dict[str, object]) -> None:
    """Validate locally; discovery schemas never authorize network retrieval."""
    pending: list[object] = [schema]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key in ("$ref", "$dynamicRef", "$recursiveRef") and (
                    not isinstance(value, str) or not value.startswith("#")
                ):
                    raise AgentError("remote tool schema references are forbidden")
                pending.append(value)
        elif isinstance(item, list):
            pending.extend(item)
    try:
        json.dumps(args, allow_nan=False)
        canonical_json(args)
        validator = validators.validator_for(schema)
        validator.check_schema(schema)
        validator(schema, registry=Registry()).validate(args)
    except Exception as exc:
        raise AgentError(f"tool arguments/schema invalid: {exc}") from exc


class RuntimeMcpExecutor:
    def __init__(self, runtime: StudyRuntime, router: ToolRouter, allowed: frozenset[str]) -> None:
        self.runtime = runtime
        self.router = router
        self.allowed = allowed
        self._lock = asyncio.Lock()

    async def check_active(self, study_id: str, attempt_id: str) -> None:
        rt = self.runtime
        await rt.ensure_lease()
        row = rt.attempts.get(attempt_id)
        if rt.study is None or rt.study.study_id != study_id:
            raise AgentError("tool study binding mismatch")
        if not isinstance(row, Mapping) or row.get("study_id") != study_id or row.get("closed") is not False:
            raise AgentError("tool attempt is not open in this study")
        task = rt.tasks.get(str(row.get("task_id")))
        if task is None:
            raise AgentError("tool attempt task missing")
        if rt._finalized or rt._study_cancelled or rt._budget_trigger_active():
            raise AgentError("tool runtime stopped")
        now = rt.clock.now()
        if now >= rt.study.deadline or (task.deadline is not None and now >= task.deadline):
            raise AgentError("budget.deadline exceeded")

    async def invoke(self, name: str, args: dict[str, object], ctx: CallCtx) -> ToolCallRef:
        async with self._lock:
            await self.check_active(ctx.study_id, ctx.attempt_id)
            if name not in self.allowed:
                raise AgentError("tool disabled by trusted authorization mask")
            row = self.runtime.attempts[ctx.attempt_id]
            task = self.runtime.tasks[str(row["task_id"])]
            if task.spec.task_key != ctx.task_key:
                raise AgentError("tool task binding mismatch")
            if task.spec.tools_hint and name not in task.spec.tools_hint:
                raise AgentError("tool exceeds accepted task capability subset")
            assert self.runtime.study is not None
            ctx.hitl_policy = self.runtime.study.hitl_policy
            resolution = self.router.resolve(ToolNeed(kind="by_name", value=name))
            if resolution.status != "tool_found" or resolution.server_name is None or resolution.tool_name is None:
                raise AgentError("tool is not an executable MCP binding")
            bound = self.router.require_tool(resolution.server_name, resolution.tool_name)
            validate_tool_arguments(bound.spec.input_schema, args)
            rows = await self.runtime.store.read_all()
            prefix = f"{ctx.attempt_id}:"
            sequences = [0]
            for event in rows:
                call_id = event.payload.get("call_id")
                if event.type == "ToolCalled":
                    call_id = event.entity_key
                elif event.type == "ArtifactRegistered":
                    producer = event.payload.get("produced_by")
                    if isinstance(producer, dict):
                        call_id = producer.get("call_id")
                if isinstance(call_id, str) and call_id.startswith(prefix):
                    suffix = call_id[len(prefix) :]
                    if suffix.isdecimal():
                        sequences.append(int(suffix))
            ctx.call_seq = max(sequences) + 1
            call_id = f"{prefix}{ctx.call_seq}"
            executor = self.runtime.executor
            if executor is None:
                raise AgentError("tool runtime has no artifact store")
            outcome: Literal["ok", "error"] = "ok"
            actual_refs: list[RecordedToolCallRef] = []

            def observed(event: EventEnvelope) -> None:
                self.runtime._project_local(event)
                if event.type == "ToolCalled" and event.entity_key == call_id:
                    actual_refs.append(RecordedToolCallRef.model_validate(event.payload["ref"]))

            try:
                deadline = min(self.runtime.study.deadline, task.deadline)
                async with asyncio.timeout((deadline - self.runtime.clock.now()).total_seconds()):
                    output = await invoke_tool(
                        bound, args, ctx, writer=self.runtime.writer, artifacts=executor.artifacts, on_event=observed
                    )
            except TimeoutError as exc:
                raise AgentError("budget.deadline exceeded during tool invocation") from exc
            except ToolError as exc:
                if exc.output is None:
                    raise
                output = exc.output
                outcome = "error"
            body = json.dumps(output.data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(actual_refs) != 1:
                raise AgentError("tool result lacks its unique recorded invocation")
            artifact = executor.artifacts.put(
                body.encode(),
                actual_refs[0],
                name=call_id,
                media_type="application/json",
                provenance="tool",
            )
            await self.runtime.append_events(
                [
                    EventEnvelope(
                        study_id=ctx.study_id,
                        type="ArtifactRegistered",
                        entity_key=artifact.artifact_id,
                        idem_key=make_idem("ArtifactRegistered", artifact.artifact_id),
                        payload=artifact.model_dump(mode="json"),
                        ts=self.runtime.clock.now(),
                        schema_version=1,
                    )
                ]
            )
            await self.check_active(ctx.study_id, ctx.attempt_id)
            return ToolCallRef(
                tool_name=name,
                resolution_status="tool_found",
                args=tuple(
                    ToolArg(key=key, value_json=json.dumps(value, allow_nan=False)) for key, value in args.items()
                ),
                result_json=body,
                call_id=call_id,
                artifact_id=artifact.artifact_id,
                outcome=outcome,
            )
