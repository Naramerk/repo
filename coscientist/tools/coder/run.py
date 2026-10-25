"""Run a coder job: JobStore lifecycle + ``run_sandbox`` + CAS.

Calls ``checks.sandbox.run_sandbox`` in a child process (no sandbox Protocol).
Deadline → ``killpg`` guest process group + ``JobTimedOut`` + retryable outcome (R4).
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import signal
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coscientist.checks.sandbox import SandboxLimits, SandboxResult, SandboxStatus, run_sandbox
from coscientist.core.events.digest import sha256_hex
from coscientist.core.events.job_events import job_is_live, job_is_terminal
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import Artifact
from coscientist.tools.contract import ToolError
from coscientist.tools.jobs import InMemoryJobStore

# One start method — spawn is safe with the join-watcher thread (no fork).
_SPAWN_CTX = mp.get_context("spawn")


class KillableProcess(Protocol):
    """Minimal process surface used by guest kill (BaseProcess satisfies this)."""

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class GuestPgidSlot(Protocol):
    """Shared int slot published by ``on_guest_started`` (mp.Value satisfies this)."""

    value: int

    def get_lock(self) -> AbstractContextManager[object]: ...


class GuestProcessGroupGone(ToolError):
    """Guest process group already reaped (ESRCH). Carry pgid/op for journaling."""

    def __init__(
        self,
        *,
        pgid: int,
        op: Literal["probe", "killpg"],
        sig_name: str,
    ) -> None:
        if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
            raise ToolError(
                "local",
                f"GuestProcessGroupGone pgid must be positive, got {pgid!r}",
            )
        self.pgid = pgid
        self.op = op
        self.sig_name = sig_name
        super().__init__(
            "local",
            f"guest process group gone: pgid={pgid} op={op} sig={sig_name}",
        )


class CoderLimitsSpec(TypedDict):
    """Wire subset of ``SandboxLimits`` stored on the job spec."""

    timeout_s: float
    memory_mb: int
    max_output_bytes: int
    pids_max: int
    rlimit_nproc: int
    allow_network: bool
    allow_cgroup_degraded: bool
    reap_timeout_s: float
    output_drain_join_s: float


class CoderOutputWire(TypedDict):
    name: str
    source: Literal["stdout", "stderr"]
    media_type: str


class CoderRequestSpec(TypedDict):
    argv: list[str]
    inputs: dict[str, str]
    env: dict[str, str]
    outputs: list[CoderOutputWire]
    limits: CoderLimitsSpec
    call_id: str


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


def _limits_to_spec(limits: SandboxLimits) -> CoderLimitsSpec:
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


def _request_spec(request: CoderRequest) -> CoderRequestSpec:
    outputs: list[CoderOutputWire] = [
        {
            "name": o.name,
            "source": o.source,
            "media_type": o.media_type,
        }
        for o in request.outputs
    ]
    return {
        "argv": list(request.argv),
        "inputs": {k: sha256_hex(v) for k, v in sorted(request.inputs.items())},
        "env": dict(sorted(request.env.items())),
        "outputs": outputs,
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


def _require_nonneg_seconds(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ToolError("local", f"{name} must be a number, got {type(value).__name__}")
    if value != value or value < 0:  # NaN or negative
        raise ToolError("local", f"{name} must be >= 0, got {value!r}")
    return float(value)


def _require_positive_seconds(name: str, value: float) -> float:
    value = _require_nonneg_seconds(name, value)
    if value == 0:
        raise ToolError("local", f"{name} must be > 0, got {value!r}")
    return value


def _require_positive_pgid(pgid: int) -> int:
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        raise ToolError("local", f"signal pgid must be positive int, got {pgid!r}")
    return pgid


def _sig_parts(
    sig: signal.Signals | Literal[0],
) -> tuple[Literal["probe", "killpg"], str, int]:
    if isinstance(sig, signal.Signals):
        return "killpg", sig.name, int(sig)
    return "probe", "0", 0


def _guest_pg_state(pgid: int) -> Literal["alive", "gone"]:
    """Guest process-group liveness via ``/proc/<pgid>`` (no killpg, no except→return)."""
    pgid = _require_positive_pgid(pgid)
    if os.path.isdir(f"/proc/{pgid}"):
        return "alive"
    return "gone"


def _signal_guest_pg(pgid: int, sig: signal.Signals | Literal[0]) -> None:
    """Sole ``os.killpg`` site. ESRCH → ``GuestProcessGroupGone`` (typed refusal upward)."""
    pgid = _require_positive_pgid(pgid)
    op, sig_name, os_sig = _sig_parts(sig)
    try:
        os.killpg(pgid, os_sig)
    except ProcessLookupError as exc:
        raise GuestProcessGroupGone(pgid=pgid, op=op, sig_name=sig_name) from exc
    except OSError as e:
        if op == "probe":
            detail = f"guest process group probe failed: pgid={pgid}: {e}"
        else:
            detail = f"killpg({sig_name}) failed: {e}"
        raise ToolError("local", detail) from e


def _signal_or_record_gone(
    jobs: InMemoryJobStore,
    jid: str,
    *,
    pgid: int,
    sig: signal.Signals | Literal[0],
) -> Literal["ok", "gone"]:
    """Typed ``ok``/``gone`` from ``/proc``; ``killpg`` only when alive.

    ``gone`` is never derived from catching ESRCH. A TOCTOU ``GuestProcessGroupGone``
    from ``killpg`` propagates as a typed refusal to the caller.
    """
    op, sig_name, _os_sig = _sig_parts(sig)
    if _guest_pg_state(pgid) == "gone":
        jobs.record_guest_process_group_gone(
            jid,
            pgid=pgid,
            op=op,
            sig_name=sig_name,
        )
        return "gone"
    if isinstance(sig, signal.Signals):
        _signal_guest_pg(pgid, sig)
    return "ok"


def _set_guest_pgid(slot: GuestPgidSlot, pgid: object) -> None:
    """Dependency injected into ``run_sandbox(on_guest_started=...)`` (spawn-safe)."""
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        raise ToolError("local", f"on_guest_started pgid must be positive int, got {pgid!r}")
    with slot.get_lock():
        if int(slot.value) == 0:
            slot.value = int(pgid)


def _sandbox_worker(
    conn: Connection,
    argv: list[str],
    limits: SandboxLimits,
    inputs: dict[str, bytes],
    env: dict[str, str],
    guest_pgid: GuestPgidSlot,
) -> None:
    """Child entry: run_sandbox; unexpected raises exit the process (parent → ToolError)."""
    try:
        result = run_sandbox(
            argv,
            limits=limits,
            inputs=inputs,
            env=env,
            on_guest_started=partial(_set_guest_pgid, guest_pgid),
        )
        conn.send(("ok", result))
    finally:
        conn.close()


async def _wait_event(event: asyncio.Event, *, timeout_s: float) -> bool:
    """True if ``event`` is set within ``timeout_s`` (no TimeoutError swallow)."""
    timeout_s = _require_nonneg_seconds("wait_timeout_s", timeout_s)
    waiter = asyncio.create_task(event.wait())
    done, pending = await asyncio.wait({waiter}, timeout=timeout_s)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return bool(done) and event.is_set()


async def _await_guest_pgid(
    slot: GuestPgidSlot,
    *,
    timeout_s: float,
    poll_interval_s: float,
) -> int:
    """Wait until sandbox publishes guest pgid; refuse if unpublished (fail-closed)."""
    timeout_s = _require_nonneg_seconds("guest_pgid_timeout_s", timeout_s)
    poll_interval_s = _require_positive_seconds("guest_pgid_poll_interval_s", poll_interval_s)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        value = int(slot.value)
        if value > 0:
            return value
        if loop.time() >= deadline:
            raise ToolError(
                "local",
                "guest pgid not published before timeout (fail-closed)",
            )
        remaining = deadline - loop.time()
        await asyncio.sleep(min(poll_interval_s, remaining))


async def _kill_sandbox_guest(
    jobs: InMemoryJobStore,
    jid: str,
    proc: KillableProcess,
    *,
    guest_pgid: int,
    term_grace_s: float,
    reap_timeout_s: float,
    guest_done: asyncio.Event,
) -> bool:
    """``killpg`` guest group (sandbox ``start_new_session``), then reap worker.

    Returns True iff the guest group is gone and the worker is reaped.
    Fail-closed: ``guest_pgid <= 0`` is not treated as gone — returns False.
    Liveness/gone is ``/proc`` via ``_signal_or_record_gone`` (no except→return).
    """
    if guest_pgid <= 0:
        if proc.is_alive():
            proc.terminate()
            if not await _wait_event(guest_done, timeout_s=reap_timeout_s) and proc.is_alive():
                proc.kill()
                await _wait_event(guest_done, timeout_s=reap_timeout_s)
        return False

    _signal_or_record_gone(jobs, jid, pgid=guest_pgid, sig=signal.SIGTERM)
    if proc.is_alive():
        proc.terminate()
    if not await _wait_event(guest_done, timeout_s=term_grace_s):
        if _signal_or_record_gone(jobs, jid, pgid=guest_pgid, sig=0) == "ok":
            _signal_or_record_gone(jobs, jid, pgid=guest_pgid, sig=signal.SIGKILL)
        if not guest_done.is_set() and proc.is_alive():
            proc.kill()
        await _wait_event(guest_done, timeout_s=reap_timeout_s)
    guest_gone = _signal_or_record_gone(jobs, jid, pgid=guest_pgid, sig=0) == "gone"
    return guest_done.is_set() and guest_gone


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


def _start_join_watcher(
    proc: BaseProcess,
    loop: asyncio.AbstractEventLoop,
    done: asyncio.Event,
) -> None:
    """Single joiner thread — avoids racing ``Process.join`` with kill."""

    def _watch() -> None:
        proc.join()
        loop.call_soon_threadsafe(done.set)

    threading.Thread(target=_watch, name="coder-sandbox-join", daemon=True).start()


async def _stop_guest_or_record(
    jobs: InMemoryJobStore,
    jid: str,
    proc: BaseProcess,
    guest_pgid_slot: GuestPgidSlot,
    guest_done: asyncio.Event,
    *,
    limits: SandboxLimits,
    unpublished_reason: str,
) -> None:
    """Resolve pgid (fail-closed), kill guest, record cancel-failed when needed."""
    try:
        pgid = await _await_guest_pgid(
            guest_pgid_slot,
            timeout_s=limits.reap_timeout_s,
            poll_interval_s=limits.watch_poll_interval_s,
        )
    except ToolError:
        await _kill_sandbox_guest(
            jobs,
            jid,
            proc,
            guest_pgid=0,
            term_grace_s=limits.term_grace_s,
            reap_timeout_s=limits.reap_timeout_s,
            guest_done=guest_done,
        )
        jobs.record_cancel_failed(jid, unpublished_reason)
    else:
        try:
            killed = await _kill_sandbox_guest(
                jobs,
                jid,
                proc,
                guest_pgid=pgid,
                term_grace_s=limits.term_grace_s,
                reap_timeout_s=limits.reap_timeout_s,
                guest_done=guest_done,
            )
        except GuestProcessGroupGone as exc:
            jobs.record_guest_process_group_gone(
                jid,
                pgid=exc.pgid,
                op=exc.op,
                sig_name=exc.sig_name,
            )
        else:
            if not killed:
                jobs.record_cancel_failed(jid, "guest_still_alive")


async def run_coder_job(
    request: CoderRequest,
    *,
    jobs: InMemoryJobStore,
    artifacts: LocalArtifactStore,
) -> CoderOutcome:
    """Submit coder job, run ``run_sandbox`` in a spawn child, register or time out."""
    spec = _request_spec(request)
    jid = jobs.submit("coder", dict(spec), request.attempt_id, request.deadline)
    jobs.mark_running(jid)

    parent_conn, child_conn = _SPAWN_CTX.Pipe(duplex=False)
    guest_pgid = _SPAWN_CTX.Value("i", 0)
    proc = _SPAWN_CTX.Process(
        target=_sandbox_worker,
        args=(
            child_conn,
            list(request.argv),
            request.limits,
            dict(request.inputs),
            dict(request.env),
            guest_pgid,
        ),
    )
    proc.start()
    child_conn.close()

    loop = asyncio.get_running_loop()
    guest_done = asyncio.Event()
    _start_join_watcher(proc, loop, guest_done)

    wait_task = asyncio.create_task(jobs.wait(jid, until=request.deadline))
    join_task = asyncio.create_task(guest_done.wait())
    finished_outcomes: list[CoderOutcome] = []
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

        if job.status == "timed_out":
            await _stop_guest_or_record(
                jobs,
                jid,
                proc,
                guest_pgid,
                guest_done,
                limits=request.limits,
                unpublished_reason="guest_pgid_unpublished",
            )
            parent_conn.close()
            outcome = _timed_out_outcome(jid, request.attempt_id, "job_deadline")
            finished_outcomes.append(outcome)
            return outcome

        if proc.is_alive():
            await _stop_guest_or_record(
                jobs,
                jid,
                proc,
                guest_pgid,
                guest_done,
                limits=request.limits,
                unpublished_reason="guest_pgid_unpublished",
            )
            raise ToolError("local", "sandbox process still alive after wait")

        if not parent_conn.poll(0):
            raise ToolError("local", "sandbox process exited without result")
        try:
            tag, payload = parent_conn.recv()
        except EOFError as e:
            raise ToolError("local", "sandbox process exited without result") from e
        parent_conn.close()
        if tag != "ok" or not isinstance(payload, SandboxResult):
            raise ToolError("local", f"bad sandbox worker payload tag={tag!r}")

        if payload.status != SandboxStatus.OK:
            mapped = sandbox_status_to_coder(payload, job_id=jid, attempt_id=request.attempt_id)
            jobs.complete(jid, status="failed", artifacts=[])
            finished_outcomes.append(mapped)
            return mapped

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
        finished_outcomes.append(outcome)
        return outcome
    finally:
        # Cleanup must not mask the try outcome or skip terminalization.
        try:
            pgid = int(guest_pgid.value)
            still_running = proc.is_alive()
            if not still_running and pgid > 0:
                still_running = _signal_or_record_gone(jobs, jid, pgid=pgid, sig=0) == "ok"
            if still_running:
                killed = await _kill_sandbox_guest(
                    jobs,
                    jid,
                    proc,
                    guest_pgid=pgid,
                    term_grace_s=request.limits.term_grace_s,
                    reap_timeout_s=request.limits.reap_timeout_s,
                    guest_done=guest_done,
                )
                if not killed:
                    jobs.record_cancel_failed(jid, "guest_still_alive")
        except GuestProcessGroupGone as exc:
            jobs.record_guest_process_group_gone(
                jid,
                pgid=exc.pgid,
                op=exc.op,
                sig_name=exc.sig_name,
            )
        except ToolError:
            jobs.record_cancel_failed(jid, "guest_cleanup_failed")
        if not finished_outcomes:
            _ensure_terminal(jobs, jid, detail="run_coder_job_exit")
