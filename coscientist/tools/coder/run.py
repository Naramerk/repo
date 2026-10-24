"""Run a coder job: JobStore lifecycle + ``checks.sandbox.run_sandbox`` + CAS.

No sandbox Protocol shim — calls ``run_sandbox`` directly (tool-gap plan).
Deadline → ``JobTimedOut`` + ``CoderOutcome(status=timed_out, retryable=True)``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coscientist.checks.sandbox import SandboxLimits, SandboxResult, SandboxStatus, run_sandbox
from coscientist.core.events.digest import sha256_hex
from coscientist.core.events.job_events import job_is_terminal
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import Artifact, FsIsolation, NetIsolation
from coscientist.tools.contract import ToolError
from coscientist.tools.jobs import InMemoryJobStore


@dataclass(frozen=True)
class CoderOutputSpec:
    """One artifact to register from sandbox stdout/stderr."""

    name: str
    source: Literal["stdout", "stderr"]
    media_type: str

    def __post_init__(self) -> None:
        if not self.name or self.name.strip() != self.name:
            raise ToolError("local", "CoderOutputSpec.name must be non-empty bare")
        if not self.media_type or self.media_type.strip() != self.media_type:
            raise ToolError("local", "CoderOutputSpec.media_type must be non-empty bare")


@dataclass(frozen=True)
class CoderRequest:
    """All fields required — no invented limits, env, or capture defaults."""

    attempt_id: str
    call_id: str
    argv: tuple[str, ...]
    inputs: Mapping[str, bytes]
    env: Mapping[str, str]
    limits: SandboxLimits
    deadline: datetime
    outputs: tuple[CoderOutputSpec, ...]
    attempt_closed: bool

    def __post_init__(self) -> None:
        if not self.attempt_id or self.attempt_id.strip() != self.attempt_id:
            raise ToolError("local", "attempt_id must be non-empty bare")
        if not self.call_id or self.call_id.strip() != self.call_id:
            raise ToolError("local", "call_id must be non-empty bare")
        if not self.argv:
            raise ToolError("local", "argv must be non-empty")
        if self.deadline.tzinfo is None:
            raise ToolError("local", "deadline must be timezone-aware")
        if not isinstance(self.attempt_closed, bool):
            raise ToolError("local", "attempt_closed must be bool")
        if not isinstance(self.limits, SandboxLimits):
            raise ToolError("local", "limits must be SandboxLimits")
        if not isinstance(self.inputs, Mapping) or not isinstance(self.env, Mapping):
            raise ToolError("local", "inputs and env must be mappings")


class CoderOutcome(BaseModel):
    """Typed coder result. ``timed_out`` ⇒ ``retryable=True`` (R4 / CAP #11)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    job_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    status: Literal["succeeded", "failed", "timed_out"]
    retryable: bool
    artifact_ids: list[str]
    detail: str = Field(min_length=1)

    @model_validator(mode="after")
    def _retryable_matches_status(self) -> CoderOutcome:
        if self.status == "timed_out" and not self.retryable:
            raise ValueError("timed_out requires retryable=True")
        if self.status == "succeeded" and self.retryable:
            raise ValueError("succeeded requires retryable=False")
        return self


_POLL_S = 0.05


def _limits_to_spec(limits: SandboxLimits) -> dict[str, Any]:
    return {
        "timeout_s": limits.timeout_s,
        "memory_mb": limits.memory_mb,
        "max_output_bytes": limits.max_output_bytes,
        "pids_max": limits.pids_max,
        "rlimit_nproc": limits.rlimit_nproc,
        "allow_network": limits.allow_network,
        "allow_cgroup_degraded": limits.allow_cgroup_degraded,
        "reap_timeout_s": limits.reap_timeout_s,
        "output_drain_join_s": limits.output_drain_join_s,
    }


def _request_spec(request: CoderRequest) -> dict[str, Any]:
    return {
        "argv": list(request.argv),
        "inputs": {k: sha256_hex(v) for k, v in sorted(request.inputs.items())},
        "env": dict(sorted(request.env.items())),
        "outputs": [
            {"name": o.name, "source": o.source, "media_type": o.media_type}
            for o in request.outputs
        ],
        "limits": _limits_to_spec(request.limits),
        "call_id": request.call_id,
    }


def _isolation(value: str) -> NetIsolation:
    if value not in ("hard", "soft", "none"):
        raise ToolError("local", f"unknown isolation {value!r}")
    return value  # type: ignore[return-value]


async def _poll_sleep() -> None:
    sleeper = asyncio.create_task(asyncio.sleep(_POLL_S))
    await sleeper


def _register_outputs(
    request: CoderRequest,
    result: SandboxResult,
    *,
    artifacts: LocalArtifactStore,
) -> list[Artifact]:
    ref = make_tool_ref(request.call_id, tool="coder")
    net = _isolation(str(result.net_isolation))
    fs: FsIsolation = _isolation(str(result.fs_isolation))
    out: list[Artifact] = []
    for spec in request.outputs:
        if spec.source == "stdout":
            content = result.stdout
        else:
            content = result.stderr
        art = artifacts.put_sandbox(
            content,
            ref,
            name=spec.name,
            media_type=spec.media_type,
            attempt_closed=request.attempt_closed,
            net_isolation=net,
            fs_isolation=fs,
        )
        out.append(art)
    return out


def _timed_out_outcome(job_id: str, attempt_id: str, detail: str) -> CoderOutcome:
    return CoderOutcome(
        job_id=job_id,
        attempt_id=attempt_id,
        status="timed_out",
        retryable=True,
        artifact_ids=[],
        detail=detail,
    )


async def run_coder_job(
    request: CoderRequest,
    *,
    jobs: InMemoryJobStore,
    artifacts: LocalArtifactStore,
) -> CoderOutcome:
    """Submit coder job, run ``run_sandbox``, register artifacts or time out (R4)."""
    spec = _request_spec(request)
    jid = jobs.submit("coder", spec, request.attempt_id, request.deadline)
    jobs.mark_running(jid)

    def _invoke() -> SandboxResult:
        return run_sandbox(
            list(request.argv),
            limits=request.limits,
            inputs=dict(request.inputs),
            env=dict(request.env),
        )

    fut = asyncio.get_running_loop().run_in_executor(None, _invoke)
    while not fut.done():
        jobs.tick()
        job = jobs.get(jid)
        if job.status == "timed_out":
            return _timed_out_outcome(jid, request.attempt_id, "job_deadline")
        if job_is_terminal(job.status):
            raise ToolError("local", f"job closed underfoot: {job.status}")
        await _poll_sleep()

    jobs.tick()
    job = jobs.get(jid)
    if job.status == "timed_out":
        return _timed_out_outcome(jid, request.attempt_id, "job_deadline")

    result = fut.result()
    if result.status in (
        SandboxStatus.TIMEOUT,
        SandboxStatus.UNAVAILABLE,
        SandboxStatus.BLOCKED,
        SandboxStatus.OOM,
    ):
        # Guest/host budget before job deadline — still a retryable failure.
        retryable = result.status == SandboxStatus.TIMEOUT
        jobs.complete(jid, status="failed", artifacts=[])
        return CoderOutcome(
            job_id=jid,
            attempt_id=request.attempt_id,
            status="failed",
            retryable=retryable,
            artifact_ids=[],
            detail=f"sandbox:{result.status.value}:{result.reason}",
        )

    if result.status != SandboxStatus.OK:
        jobs.complete(jid, status="failed", artifacts=[])
        return CoderOutcome(
            job_id=jid,
            attempt_id=request.attempt_id,
            status="failed",
            retryable=False,
            artifact_ids=[],
            detail=f"sandbox:{result.status.value}:{result.reason}",
        )

    registered = _register_outputs(request, result, artifacts=artifacts)
    ids = [a.artifact_id for a in registered]
    for art in registered:
        if not art.sha256 or len(art.sha256) != 64:
            raise ToolError("local", f"artifact missing sha256: {art.artifact_id}")
    jobs.complete(jid, status="succeeded", artifacts=ids)
    return CoderOutcome(
        job_id=jid,
        attempt_id=request.attempt_id,
        status="succeeded",
        retryable=False,
        artifact_ids=ids,
        detail="ok",
    )
