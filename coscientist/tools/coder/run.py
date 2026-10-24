"""Run a coder job: JobStore lifecycle + ``run_sandbox`` + CAS.

Calls ``checks.sandbox.run_sandbox`` in a child process (no sandbox Protocol).
Deadline → kill guest process group + ``JobTimedOut`` + retryable outcome (R4).
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from multiprocessing.connection import Connection
from multiprocessing.context import ForkContext, SpawnContext
from multiprocessing.process import BaseProcess
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coscientist.checks.sandbox import SandboxLimits, SandboxResult, SandboxStatus, run_sandbox
from coscientist.core.events.digest import sha256_hex
from coscientist.core.events.job_events import job_is_live, job_is_terminal
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import Artifact
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
    """All fields required — no invented limits/env. ``attempt_is_closed`` read at put."""

    attempt_id: str
    call_id: str
    argv: tuple[str, ...]
    inputs: Mapping[str, bytes]
    env: Mapping[str, str]
    limits: SandboxLimits
    deadline: datetime
    outputs: tuple[CoderOutputSpec, ...]
    attempt_is_closed: Callable[[], bool]

    def __post_init__(self) -> None:
        if not self.attempt_id or self.attempt_id.strip() != self.attempt_id:
            raise ToolError("local", "attempt_id must be non-empty bare")
        if not self.call_id or self.call_id.strip() != self.call_id:
            raise ToolError("local", "call_id must be non-empty bare")
        if not self.argv:
            raise ToolError("local", "argv must be non-empty")
        if self.deadline.tzinfo is None:
            raise ToolError("local", "deadline must be timezone-aware")
        if not callable(self.attempt_is_closed):
            raise ToolError("local", "attempt_is_closed must be callable")
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
        "outputs": [{"name": o.name, "source": o.source, "media_type": o.media_type} for o in request.outputs],
        "limits": _limits_to_spec(request.limits),
        "call_id": request.call_id,
    }


def sandbox_status_to_coder(
    result: SandboxResult,
    *,
    job_id: str,
    attempt_id: str,
) -> CoderOutcome:
    """Map ``SandboxStatus`` → coder failed/retryable (pure; tested)."""
    if result.status == SandboxStatus.OK:
        raise ToolError("local", "sandbox_status_to_coder: OK is not a failure mapping")
    retryable = result.status == SandboxStatus.TIMEOUT
    return CoderOutcome(
        job_id=job_id,
        attempt_id=attempt_id,
        status="failed",
        retryable=retryable,
        artifact_ids=[],
        detail=f"sandbox:{result.status.value}:{result.reason}",
    )


def _timed_out_outcome(job_id: str, attempt_id: str, detail: str) -> CoderOutcome:
    return CoderOutcome(
        job_id=job_id,
        attempt_id=attempt_id,
        status="timed_out",
        retryable=True,
        artifact_ids=[],
        detail=detail,
    )


def _sandbox_worker(
    conn: Connection,
    argv: list[str],
    limits: SandboxLimits,
    inputs: dict[str, bytes],
    env: dict[str, str],
) -> None:
    try:
        result = run_sandbox(argv, limits=limits, inputs=inputs, env=env)
        conn.send(("ok", result))
    except BaseException as exc:  # audit-ok[broad_except]: pipe must report any worker failure to parent
        conn.send(("err", exc))
    finally:
        conn.close()


async def _kill_sandbox_process(
    proc: BaseProcess,
    *,
    reap_timeout_s: float,
    guest_done: asyncio.Event,
) -> bool:
    """Signal guest then await the sole join-watcher; True iff watcher reaped.

    Only the join-watcher thread calls ``Process.join`` / waitpid — callers must
    not ``poll``/``join`` in parallel (races leave ``is_alive()`` wrong).
    """
    if guest_done.is_set():
        return True
    proc.terminate()
    try:
        await asyncio.wait_for(guest_done.wait(), timeout=reap_timeout_s)
    except TimeoutError:
        pass  # audit-ok[except_silent_pass]: term grace elapsed → escalate to kill
    if guest_done.is_set():
        return True
    proc.kill()
    try:
        await asyncio.wait_for(guest_done.wait(), timeout=reap_timeout_s)
    except TimeoutError:
        pass  # audit-ok[except_silent_pass]: kill grace elapsed → report cancel-failed
    return guest_done.is_set()


def _register_outputs(
    request: CoderRequest,
    result: SandboxResult,
    *,
    artifacts: LocalArtifactStore,
) -> list[Artifact]:
    closed = request.attempt_is_closed()
    if not isinstance(closed, bool):
        raise ToolError("local", "attempt_is_closed() must return bool")
    ref = make_tool_ref(request.call_id, tool="coder")
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
            attempt_closed=closed,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
        )
        out.append(art)
    return out


def _ensure_terminal(jobs: InMemoryJobStore, job_id: str, *, detail: str) -> None:
    job = jobs.get(job_id)
    if job_is_live(job.status):
        jobs.complete(job_id, status="failed", artifacts=[])
        return
    if not job_is_terminal(job.status):
        raise ToolError("local", f"job not terminal after ensure: {job.status} ({detail})")


def _start_join_watcher(proc: BaseProcess, loop: asyncio.AbstractEventLoop, done: asyncio.Event) -> None:
    """Single joiner thread — avoids racing ``Process.join`` with kill."""

    def _watch() -> None:
        proc.join()
        loop.call_soon_threadsafe(done.set)

    threading.Thread(target=_watch, name="coder-sandbox-join", daemon=True).start()


async def run_coder_job(
    request: CoderRequest,
    *,
    jobs: InMemoryJobStore,
    artifacts: LocalArtifactStore,
) -> CoderOutcome:
    """Submit coder job, run ``run_sandbox`` in a child process, register or time out."""
    spec = _request_spec(request)
    jid = jobs.submit("coder", spec, request.attempt_id, request.deadline)
    jobs.mark_running(jid)

    # fork: inherit test monkeypatches; spawn fallback when fork unavailable.
    methods = mp.get_all_start_methods()
    ctx = cast(
        ForkContext | SpawnContext,
        mp.get_context("fork" if "fork" in methods else "spawn"),
    )
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_sandbox_worker,
        args=(
            child_conn,
            list(request.argv),
            request.limits,
            dict(request.inputs),
            dict(request.env),
        ),
    )
    proc.start()
    child_conn.close()

    loop = asyncio.get_running_loop()
    guest_done = asyncio.Event()
    _start_join_watcher(proc, loop, guest_done)

    wait_task = asyncio.create_task(jobs.wait(jid, until=request.deadline))
    join_task = asyncio.create_task(guest_done.wait())
    outcome: CoderOutcome | None = None  # audit-ok[field_default]: mutable slot until try returns
    try:
        finished, pending = await asyncio.wait(
            {wait_task, join_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        job = jobs.get(jid)
        if wait_task in finished and not wait_task.cancelled():
            job = wait_task.result()

        timed_out = job.status == "timed_out" or (
            wait_task in finished and not wait_task.cancelled() and wait_task.result().status == "timed_out"
        )
        if timed_out:
            killed = await _kill_sandbox_process(
                proc,
                reap_timeout_s=request.limits.reap_timeout_s,
                guest_done=guest_done,
            )
            if not killed:
                jobs.record_cancel_failed(jid, "guest_still_alive")
            parent_conn.close()
            outcome = _timed_out_outcome(jid, request.attempt_id, "job_deadline")
            return outcome

        if proc.is_alive():
            killed = await _kill_sandbox_process(
                proc,
                reap_timeout_s=request.limits.reap_timeout_s,
                guest_done=guest_done,
            )
            if not killed:
                jobs.record_cancel_failed(jid, "guest_still_alive")
            raise ToolError("local", "sandbox process still alive after wait")

        if not parent_conn.poll(0):
            raise ToolError("local", "sandbox process exited without result")
        tag, payload = parent_conn.recv()
        parent_conn.close()
        if tag == "err":
            raise payload
        if tag != "ok" or not isinstance(payload, SandboxResult):
            raise ToolError("local", f"bad sandbox worker payload tag={tag!r}")

        if payload.status != SandboxStatus.OK:
            mapped = sandbox_status_to_coder(payload, job_id=jid, attempt_id=request.attempt_id)
            jobs.complete(jid, status="failed", artifacts=[])
            outcome = mapped
            return outcome

        registered = _register_outputs(request, payload, artifacts=artifacts)
        ids = [a.artifact_id for a in registered]
        for art in registered:
            if not art.sha256 or len(art.sha256) != 64:
                raise ToolError("local", f"artifact missing sha256: {art.artifact_id}")
        jobs.complete(jid, status="succeeded", artifacts=ids)
        outcome = CoderOutcome(
            job_id=jid,
            attempt_id=request.attempt_id,
            status="succeeded",
            retryable=False,
            artifact_ids=ids,
            detail="ok",
        )
        return outcome
    finally:
        if proc.is_alive():
            await _kill_sandbox_process(
                proc,
                reap_timeout_s=request.limits.reap_timeout_s,
                guest_done=guest_done,
            )
        if outcome is None:
            _ensure_terminal(jobs, jid, detail="run_coder_job_exit")
