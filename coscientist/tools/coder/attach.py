"""Attach ``run_coder_job`` to the study ``invoke_tool`` path (DESIGN §6.3–6.4).

Hard Linux sandbox preflight: refuse when bwrap / hard isolation / cgroup
delegation is unavailable. Does **not** bypass isolation
(``allow_cgroup_degraded`` forbidden on this path).

Resolution seam (G3): ``ToolRouter.resolve(by_name="coder")`` returns
``Resolution(status="coder")`` only when the router has an explicit coder
route attached (``attach_coder_route``; backend via ``resolve_coder_backend``).
Journalled ``ToolResolved(status="coder")`` stays a #65 payload-Literal hard
dep; this path journals ``ToolCalled`` + ``EffectIntended``/``EffectDone`` via
``invoke_tool`` instead.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
from coscientist.tools.coder.local import (
    CoderBackend,
    require_local_python,
    resolve_coder_backend,
    run_local_coder_job,
)
from coscientist.tools.coder.remote_contract import RemoteCoderTaskRequest
from coscientist.tools.coder.remote_runtime import RemoteCoderRuntime
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


def _attempt_is_closed_from(extra: dict[str, Any]) -> Callable[[], bool]:
    """Injected attempt-close probe for the remote path (no CoderRequest field)."""
    probe = extra.get("attempt_is_closed")
    if not callable(probe):
        raise ToolError("local", "remote coder requires ctx.extra['attempt_is_closed'] callable")
    return probe


async def _run_remote_coder_tool(
    request: CoderRequest | RemoteCoderTaskRequest,
    extra: dict[str, Any],
    *,
    jobs: InMemoryJobStore,
    artifacts: LocalArtifactStore,
) -> CoderOutcome:
    """Remote backend route inside ``CoderTool.call`` (typed deps, no fallback)."""
    if not isinstance(request, RemoteCoderTaskRequest):
        raise ToolError(
            "local",
            "remote-openhands backend requires a RemoteCoderTaskRequest "
            "(no argv is invented for the remote path)",
        )
    runtime = extra.get("remote_runtime")
    if not isinstance(runtime, RemoteCoderRuntime):
        raise ToolError("local", "remote coder requires ctx.extra['remote_runtime']")
    names = extra.get("remote_output_names")
    if not isinstance(names, Mapping):
        raise ToolError("local", "remote coder requires ctx.extra['remote_output_names'] mapping")
    return await runtime.execute(
        request,
        jobs=jobs,
        artifacts=artifacts,
        attempt_is_closed=_attempt_is_closed_from(extra),
        output_names=dict(names),
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
        if not isinstance(ctx.extra, dict):
            raise ToolError("local", "coder tool requires ctx.extra mapping")
        request = ctx.extra.get("coder_request")
        if not isinstance(request, CoderRequest | RemoteCoderTaskRequest):
            raise ToolError("local", "coder tool requires ctx.extra['coder_request']")
        jobs = ctx.extra.get("jobs")
        if not isinstance(jobs, InMemoryJobStore):
            raise ToolError("local", "coder tool requires ctx.extra['jobs']")
        artifacts = ctx.extra.get("artifacts")
        if not isinstance(artifacts, LocalArtifactStore):
            raise ToolError("local", "coder tool requires ctx.extra['artifacts']")

        if "call_id" not in args or "attempt_id" not in args:
            raise ToolError("local", "coder tool requires call_id and attempt_id args")
        arg_call = args["call_id"]
        arg_attempt = args["attempt_id"]
        if not isinstance(arg_call, str) or not arg_call or arg_call.strip() != arg_call:
            raise ToolError("local", "coder tool call_id must be non-empty bare str")
        if (
            not isinstance(arg_attempt, str)
            or not arg_attempt
            or arg_attempt.strip() != arg_attempt
        ):
            raise ToolError("local", "coder tool attempt_id must be non-empty bare str")
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

        backend = resolve_coder_backend(ctx.extra.get("coder_backend"))
        if backend == "remote-openhands":
            outcome = await _run_remote_coder_tool(
                request,
                ctx.extra,
                jobs=jobs,
                artifacts=artifacts,
            )
        elif backend == "bwrap":
            if not isinstance(request, CoderRequest):
                raise ToolError("local", "bwrap backend requires a local CoderRequest")
            require_supported_linux_sandbox(allow_network=request.limits.allow_network)
            _refuse_degraded(request)
            outcome = await run_coder_job(request, jobs=jobs, artifacts=artifacts)
        else:
            if not isinstance(request, CoderRequest):
                raise ToolError("local", "local-python backend requires a local CoderRequest")
            require_local_python(request)
            outcome = await run_local_coder_job(
                request, jobs=jobs, artifacts=artifacts
            )
        arts = []
        for aid in outcome.artifact_ids:
            meta = artifacts.get_meta(aid)
            if meta is None:
                raise ToolError("local", f"coder tool missing artifact meta: {aid}")
            arts.append(meta)
        return ToolOutput(data=outcome.model_dump(mode="json"), artifacts=arts)


CODER_TOOL = CoderTool()


async def invoke_coder_for_study(
    request: CoderRequest | RemoteCoderTaskRequest,
    *,
    backend: CoderBackend,
    jobs: InMemoryJobStore,
    writer: StudyWriter,
    artifacts: LocalArtifactStore,
    ctx: CallCtx,
    remote_runtime: RemoteCoderRuntime | None = None,
    remote_output_names: Mapping[str, str] | None = None,
    attempt_is_closed: Callable[[], bool] | None = None,
) -> tuple[CoderOutcome, ToolOutput]:
    """Study/workflow entry: explicit backend preflight, then ``invoke_tool(coder)``.

    ``backend`` is explicit config (``bwrap`` Linux/CI hard sandbox,
    ``local-python`` macOS/dev with no isolation claims, or ``remote-openhands``
    legacy service with a model-generated task). Preserves #150 gorilla closes
    inside the runners. This wrapper only adds the invoke / registry seam and
    the per-backend refuse gates. The remote path requires the composed
    ``remote_runtime``, the validated ``remote_output_names`` (path → expected
    output name) and an ``attempt_is_closed`` probe — typed refuse otherwise.
    """
    backend = resolve_coder_backend(backend)
    if backend == "remote-openhands":
        if not isinstance(request, RemoteCoderTaskRequest):
            raise ToolError(
                "local",
                "remote-openhands invoke requires a RemoteCoderTaskRequest",
            )
        if not isinstance(remote_runtime, RemoteCoderRuntime):
            raise ToolError("local", "remote-openhands invoke requires remote_runtime")
        if not isinstance(remote_output_names, Mapping):
            raise ToolError("local", "remote-openhands invoke requires remote_output_names")
        if not callable(attempt_is_closed):
            raise ToolError("local", "remote-openhands invoke requires attempt_is_closed")
    elif backend == "bwrap":
        if not isinstance(request, CoderRequest):
            raise ToolError("local", "bwrap invoke requires a local CoderRequest")
        require_supported_linux_sandbox(allow_network=request.limits.allow_network)
        _refuse_degraded(request)
    else:
        if not isinstance(request, CoderRequest):
            raise ToolError("local", "local-python invoke requires a local CoderRequest")
        require_local_python(request)
    if not isinstance(ctx.extra, dict):
        raise ToolError("local", "coder workflow requires ctx.extra mapping")
    extra = dict(ctx.extra)
    extra["coder_request"] = request
    extra["coder_backend"] = backend
    extra["jobs"] = jobs
    extra["artifacts"] = artifacts
    if backend == "remote-openhands":
        assert remote_runtime is not None and remote_output_names is not None
        assert attempt_is_closed is not None
        extra["remote_runtime"] = remote_runtime
        extra["remote_output_names"] = dict(remote_output_names)
        extra["attempt_is_closed"] = attempt_is_closed
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
