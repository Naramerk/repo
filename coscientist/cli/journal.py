"""Map agent runs → ``AgentTaskOutput`` and journal via ``Executor.record``."""

from __future__ import annotations

import json
from typing import Any

from coscientist.agents.research_spec import ResearchSpec
from coscientist.agents.schemas import AgentRun, ExecutorRouteOut, JsonEntry
from coscientist.api.runtime import StudyRuntime
from coscientist.cli.errors import CliRefuse
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle


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


async def record_research_spec(rt: StudyRuntime, aid: str, spec: ResearchSpec) -> None:
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


async def record_stage_pass(rt: StudyRuntime, aid: str, *, stage: str) -> None:
    """Journal HYP/LIT SUCCESS when Need is off and hypothesizer/researcher absent.

    This is the journal path (``Executor.record``), not cancel. Scientific HYP/LIT
    agents are not wired on #146 — CLI refuses if planner sets ``needed=True``.
    """
    if rt.executor is None:
        raise CliRefuse("runtime has no executor", code=4)
    payload = {"stage": stage, "status": "pass", "agent": None}
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    art = rt.executor.artifacts.put(
        content,
        make_tool_ref(f"{aid}:{stage}"),
        name="main",
        media_type="application/json",
    )
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary=f"{stage} pass (Need off; hypothesizer/researcher not wired)",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values=payload,
        ),
    )


async def record_executor_route(rt: StudyRuntime, aid: str, run: AgentRun) -> None:
    """Map ``ExecutorRouteOut`` + tool results → ``AgentTaskOutput`` and record."""
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
    values = _parse_values(out.values)
    outputs: dict[str, ArtifactHandle] = {}
    # Prefer tool result payloads as named artifacts; first tool also fills ``main``.
    for idx, ref in enumerate(run.tool_calls):
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
        if idx == 0 and "main" not in outputs:
            main = rt.executor.artifacts.put(
                body,
                make_tool_ref(f"{aid}:main"),
                name="main",
                media_type="application/json",
            )
            outputs["main"] = ArtifactHandle(artifact_id=main.artifact_id, name="main")
    if "main" not in outputs:
        # No tools — persist structured values as main artifact.
        body = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        main = rt.executor.artifacts.put(
            body,
            make_tool_ref(f"{aid}:main"),
            name="main",
            media_type="application/json",
        )
        outputs["main"] = ArtifactHandle(artifact_id=main.artifact_id, name="main")
    attempt = rt.attempts.get(aid)
    if not isinstance(attempt, dict):
        attempt = {}
    task_id = attempt.get("task_id")
    task = rt.tasks.get(task_id) if type(task_id) is str else None
    if task is not None:
        addresses = list(task.spec.addresses)
    else:
        addresses = []
    claims = [{"id": f"c_{aid}_{q}", "target": q} for q in addresses]
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary=out.summary,
            outputs=outputs,
            values=values,
            claims=claims,
        ),
    )
