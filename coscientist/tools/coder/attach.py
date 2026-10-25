"""Attach ``run_coder_job`` to the study ``invoke_tool`` path (DESIGN §6.3–6.4).

Hard Linux sandbox preflight: refuse when bwrap / hard isolation / cgroup
delegation is unavailable. Does **not** bypass isolation
(``allow_cgroup_degraded`` forbidden on this path).

Does **not** invent ``ToolResolved(status=coder)`` — that status is a hard dep
on #65 payload Literal extension + alembic policy / profile. Callers with only
``tool_missing`` must refuse rather than invent a coder resolution.
"""

from __future__ import annotations

from typing import Any

from coscientist.checks.sandbox import require_cgroup_delegation
from coscientist.checks.sandbox_probe import (
    bwrap_available,
    isolation_fully_hard,
    resolve_isolation,
)
from coscientist.core.events.errors import SandboxInfraError
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.tools.coder.run import CoderOutcome, CoderRequest, run_coder_job
from coscientist.tools.contract import CallCtx, ToolError, ToolOutput, ToolSpec
from coscientist.tools.effects import invoke_tool
from coscientist.tools.jobs import InMemoryJobStore

_CODER_NAME = "coder"


def require_supported_linux_sandbox(*, allow_network: bool) -> None:
    """Fail-closed host preflight for the coder workflow path.

    Raises ``ToolError(kind=local)`` when bwrap, hard net+fs isolation, or
    cgroup delegation is unavailable. Never enables degraded cgroup mode.
    """
    if not isinstance(allow_network, bool):
        raise ToolError(
            "local",
            f"coder sandbox refuse: allow_network must be bool, got {type(allow_network).__name__}",
        )
    if not bwrap_available():
        raise ToolError("local", "coder sandbox refuse: bwrap unavailable")
    net, fs = resolve_isolation(allow_network=allow_network)
    if not isolation_fully_hard(net, fs):
        raise ToolError(
            "local",
            f"coder sandbox refuse: isolation not hard net={net} fs={fs}",
        )
    try:
        require_cgroup_delegation()
    except SandboxInfraError as exc:
        raise ToolError(
            "local",
            f"coder sandbox refuse: cgroup unavailable:{exc}",
        ) from exc


def _refuse_degraded(request: CoderRequest) -> None:
    if request.limits.allow_cgroup_degraded:
        raise ToolError(
            "local",
            "coder workflow refuse: allow_cgroup_degraded bypass forbidden",
        )


class CoderTool:
    """Write-effect tool: ``run_coder_job`` under hard sandbox preflight."""

    spec = ToolSpec(
        name=_CODER_NAME,
        effect="write",
        timeout_s=120,
        max_retries=0,
        provider="coder",
        input_schema={
            "type": "object",
            "required": ["call_id", "attempt_id"],
            "properties": {
                "call_id": {"type": "string"},
                "attempt_id": {"type": "string"},
            },
        },
    )

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
        request = (ctx.extra or {}).get("coder_request")
        if not isinstance(request, CoderRequest):
            raise ToolError("local", "coder tool requires ctx.extra['coder_request']")
        jobs = (ctx.extra or {}).get("jobs")
        if not isinstance(jobs, InMemoryJobStore):
            raise ToolError("local", "coder tool requires ctx.extra['jobs']")
        artifacts = (ctx.extra or {}).get("artifacts")
        if not isinstance(artifacts, LocalArtifactStore):
            raise ToolError("local", "coder tool requires ctx.extra['artifacts']")

        arg_call = str(args.get("call_id") or "")
        arg_attempt = str(args.get("attempt_id") or "")
        if arg_call != request.call_id:
            raise ToolError(
                "local",
                f"coder tool call_id {arg_call!r} does not match request {request.call_id!r}",
            )
        if arg_attempt != request.attempt_id:
            raise ToolError(
                "local",
                f"coder tool attempt_id {arg_attempt!r} does not match "
                f"request {request.attempt_id!r}",
            )
        if arg_attempt != ctx.attempt_id:
            raise ToolError(
                "local",
                f"coder tool attempt_id {arg_attempt!r} does not match "
                f"ctx.attempt_id {ctx.attempt_id!r}",
            )

        require_supported_linux_sandbox(allow_network=request.limits.allow_network)
        _refuse_degraded(request)

        outcome = await run_coder_job(request, jobs=jobs, artifacts=artifacts)
        arts = []
        for aid in outcome.artifact_ids:
            meta = artifacts.get_meta(aid)
            if meta is None:
                raise ToolError("local", f"coder tool missing artifact meta: {aid}")
            arts.append(meta)
        return ToolOutput(data=outcome.model_dump(mode="json"), artifacts=arts)


CODER_TOOL = CoderTool()


async def invoke_coder_for_study(
    request: CoderRequest,
    *,
    jobs: InMemoryJobStore,
    writer: StudyWriter,
    artifacts: LocalArtifactStore,
    ctx: CallCtx,
) -> tuple[CoderOutcome, ToolOutput]:
    """Study/workflow entry: hard preflight, then ``invoke_tool(coder)``.

    Preserves #150 gorilla closes inside ``run_coder_job`` (cgroup.events
    liveness, ``GuestProcessGroupGone`` journal+raise). This wrapper only adds
    the invoke/registry seam and host refuse gates.
    """
    require_supported_linux_sandbox(allow_network=request.limits.allow_network)
    _refuse_degraded(request)
    extra = dict(ctx.extra or {})
    extra["coder_request"] = request
    extra["jobs"] = jobs
    extra["artifacts"] = artifacts
    ctx.extra = extra
    output = await invoke_tool(
        CODER_TOOL,
        {"call_id": request.call_id, "attempt_id": request.attempt_id},
        ctx,
        writer=writer,
        artifacts=artifacts,
    )
    if not isinstance(output.data, dict):
        raise ToolError("local", "coder invoke: ToolOutput.data must be a mapping")
    outcome = CoderOutcome.model_validate(output.data)
    return outcome, output
