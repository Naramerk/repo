"""local-python coder backend: explicit dev/macOS path, no sandbox claims.

Honest guarantees (DESIGN M3 coder / CAP #11): cwd-isolated workspace dir,
wall-time deadline, output size caps, exit code + captured stdout/stderr as
the typed job result. **No network isolation** — the child inherits the host
network (needed by e.g. alembic_nomcp cloning GitHub), so ``allow_network``
must be True on this backend (fail-closed refuse otherwise). ``resource``
limits (RLIMIT_AS/RLIMIT_CPU) are best-effort; macOS may ignore RLIMIT_AS.

Selection is explicit config only (``resolve_coder_backend`` refuses anything
that is not exactly ``"bwrap"``, ``"local-python"`` or ``"remote-openhands"``)
— never a platform guess. The bwrap backend stays the CI gate (Linux hard
isolation). ``remote-openhands`` is owned by ``tools.coder.remote_runtime`` +
``attach`` (model-generated task content on the legacy OpenHands service);
this module owns only the two local backends.
"""

from __future__ import annotations

import asyncio
import os
import resource
import shutil
import signal
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from coscientist.checks.sandbox import SandboxLimits, SandboxResult, SandboxStatus
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.tools.coder.run import (
    CoderOutcome,
    CoderRequest,
    _ensure_terminal,
    _register_outputs,
    _request_spec,
    sandbox_status_to_coder,
)
from coscientist.tools.contract import ToolError
from coscientist.tools.jobs import InMemoryJobStore

CoderBackend = Literal["bwrap", "local-python", "remote-openhands"]

_RLIMIT_NAMES = {
    resource.RLIMIT_AS: "RLIMIT_AS",
    resource.RLIMIT_CPU: "RLIMIT_CPU",
}

_READ_CHUNK = 8192


def resolve_coder_backend(configured: object) -> CoderBackend:
    """Explicit config only — refuse (typed) when no valid backend is set."""
    if configured == "bwrap":
        return "bwrap"
    if configured == "local-python":
        return "local-python"
    if configured == "remote-openhands":
        return "remote-openhands"
    raise ToolError(
        "local",
        "coder backend refuse: explicit backend required — "
        "'bwrap' (Linux/CI hard sandbox), 'local-python' (macOS/dev, no isolation) "
        "or 'remote-openhands' (legacy OpenHands service, model-generated task)",
    )


def require_local_python(request: CoderRequest) -> None:
    """Fail-closed local-python preflight: no weakened gates.

    Refuses ``allow_network=False`` (this backend cannot block network) and
    the ``allow_cgroup_degraded`` bypass (same rule as the bwrap path).
    """
    if not request.limits.allow_network:
        raise ToolError(
            "local",
            "local-python refuse: network is NOT isolated on this backend; "
            "allow_network=False cannot be honored",
        )
    if request.limits.allow_cgroup_degraded:
        raise ToolError(
            "local",
            "coder workflow refuse: allow_cgroup_degraded bypass forbidden",
        )


def _best_effort_rlimits(limits: SandboxLimits) -> Callable[[], None]:
    """preexec hook: RLIMIT_AS/RLIMIT_CPU best-effort (macOS may ignore).

    A refused limit is recorded on the child's stderr (visible in the typed
    job result) instead of being silently swallowed — the backend documents
    rlimits as best-effort, and this makes every miss observable.
    """

    def _apply_rlimit(res: int, value: tuple[int, int], failed: list[str]) -> None:
        try:
            resource.setrlimit(res, value)
        except (ValueError, OSError):
            failed.append(_RLIMIT_NAMES[res])

    def _apply() -> None:
        mem = limits.memory_mb * 1024 * 1024
        cpu = max(1, int(limits.timeout_s))
        failed: list[str] = []
        _apply_rlimit(resource.RLIMIT_AS, (mem, mem), failed)
        _apply_rlimit(resource.RLIMIT_CPU, (cpu, cpu + 1), failed)
        if failed:
            os.write(2, f"local-python: rlimit not applied: {','.join(failed)}\n".encode())

    return _apply


def _check_argv(argv: tuple[str, ...]) -> None:
    if not argv:
        raise ToolError("local", "local-python argv must be non-empty")
    if shutil.which(argv[0]) is None:
        raise ToolError("local", f"local-python executable not found: {argv[0]!r}")


def _write_inputs(workspace: Path, inputs: dict[str, bytes]) -> None:
    for name, content in inputs.items():
        if not name or name.strip() != name or Path(name).name != name:
            raise ToolError("local", f"local-python input name must be bare: {name!r}")
        (workspace / name).write_bytes(content)


async def _read_capped(
    reader: asyncio.StreamReader,
    cap: int,
    overflow: list[bool],
    overflow_event: asyncio.Event,
) -> bytes:
    """Read until EOF; truncate the final chunk at ``cap`` (sandbox parity).

    Sets ``overflow[0]`` (and ``overflow_event``) as soon as the cap is
    exceeded, then keeps draining and discarding until EOF so a flooding
    guest never blocks on a full pipe (the overflow flag is then classifiable
    even under a concurrent deadline).
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await reader.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            overflow[0] = True
            overflow_event.set()
            keep = cap - (total - len(chunk))
            if keep > 0:
                chunks.append(chunk[:keep])
            while True:
                if not await reader.read(_READ_CHUNK):
                    break
            break
        chunks.append(chunk)
    return b"".join(chunks)


_EXIT_POLL_INTERVAL_S = 0.05


async def _poll_exit(
    proc: asyncio.subprocess.Process,
    *,
    timeout_s: float | None,
) -> int | None:
    """Wait for ``returncode`` by polling (see run_local_coder_job note).

    Returns None when ``timeout_s`` elapses first (guest unreaped).
    """
    loop = asyncio.get_running_loop()
    deadline = None if timeout_s is None else loop.time() + timeout_s
    while proc.returncode is None:
        if deadline is not None and loop.time() >= deadline:
            return None
        await asyncio.sleep(_EXIT_POLL_INTERVAL_S)
    return proc.returncode


def _close_pipes(proc: asyncio.subprocess.Process) -> None:
    """Close pipe transports while the loop still runs (no GC-after-close noise)."""
    # asyncio.subprocess.Process keeps the private transport on the instance dict.
    transport = vars(proc).get("_transport")
    if transport is not None:
        transport.close()  # loop is running here (async finally)


def _kill_guest(
    proc: asyncio.subprocess.Process,
    *,
    jobs: InMemoryJobStore,
    jid: str,
) -> None:
    """SIGKILL the guest process group (start_new_session). ESRCH → journaled."""
    if proc.returncode is not None or proc.pid is None or proc.pid <= 0:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        # Group already reaped — journal like the bwrap path (no silent drop).
        jobs.record_guest_process_group_gone(
            jid, pgid=proc.pid, op="killpg", sig_name="SIGKILL"
        )


def _or_empty(data: bytes | None) -> bytes:
    """Stuck drains contribute no bytes (never invent sink contents)."""
    if data is None:
        return b""
    return data


def _to_result(
    status: SandboxStatus,
    *,
    exit_code: int | None,
    stdout: bytes,
    stderr: bytes,
    reason: str,
    duration_s: float,
    drain_stuck: bool = False,
) -> SandboxResult:
    return SandboxResult(
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        reason=reason,
        duration_s=duration_s,
        cgroup_limits_held=False,
        net_isolation="none",
        fs_isolation="none",
        oom_detectable=False,
        output_drain_failed=drain_stuck,
        note="local-python backend: no net/fs isolation (explicit config)",
    )


async def run_local_coder_job(
    request: CoderRequest,
    *,
    jobs: InMemoryJobStore,
    artifacts: LocalArtifactStore,
    workspace_root: Path | None = None,
) -> CoderOutcome:
    """Run ``request.argv`` as a local subprocess under explicit dev budgets."""
    require_local_python(request)
    _check_argv(request.argv)

    # Wall budget: limits.timeout_s folded into the JobStore deadline so the
    # timeout path (JobTimedOut + retryable outcome) is the bwrap path's.
    effective_deadline = min(
        request.deadline,
        datetime.now(UTC) + timedelta(seconds=request.limits.timeout_s),
    )
    jid = jobs.submit(
        "coder", dict(_request_spec(request)), request.attempt_id, effective_deadline
    )
    jobs.mark_running(jid)

    workspace = Path(tempfile.mkdtemp(prefix="coder-local-", dir=workspace_root))
    proc: asyncio.subprocess.Process | None
    proc = None
    try:
        _write_inputs(workspace, dict(request.inputs))
        started = asyncio.get_running_loop().time()
        proc = await asyncio.create_subprocess_exec(
            *request.argv,
            cwd=workspace,
            env={**os.environ, **dict(request.env)},
            start_new_session=True,
            preexec_fn=_best_effort_rlimits(request.limits),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if proc.stdout is None or proc.stderr is None:  # PIPE above — fail-closed
            raise ToolError("local", "local-python stdout/stderr pipes missing")
        out_overflow: list[bool] = [False]
        err_overflow: list[bool] = [False]
        overflow_event = asyncio.Event()
        out_task = asyncio.create_task(
            _read_capped(
                proc.stdout, request.limits.max_output_bytes, out_overflow, overflow_event
            )
        )
        err_task = asyncio.create_task(
            _read_capped(
                proc.stderr, request.limits.max_output_bytes, err_overflow, overflow_event
            )
        )

        wait_task = asyncio.create_task(jobs.wait(jid, until=request.deadline))
        # Poll returncode, not proc.wait(): asyncio wait() resolves only after
        # all pipe transports see EOF — a pipe-holding grandchild would else
        # mask a real guest exit as a wall timeout.
        wall_task = asyncio.create_task(_poll_exit(proc, timeout_s=None))
        flood_task = asyncio.create_task(overflow_event.wait())
        done, pending = await asyncio.wait(
            {wait_task, wall_task, flood_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        async def _reap_guest() -> int | None:
            _kill_guest(proc, jobs=jobs, jid=jid)
            if await _poll_exit(proc, timeout_s=request.limits.reap_timeout_s) is None:
                jobs.record_cancel_failed(jid, "local_python_guest_still_alive")
            return proc.returncode

        async def _drain_or_none(task: asyncio.Task[bytes]) -> bytes | None:
            """Bounded drain (``output_drain_join_s``); None = drain stuck.

            A grandchild outside the killed process group can hold the pipe
            open forever — bwrap bounds this the same way and classifies the
            stuck sink as infra (``output_drain_failed``), never hangs.
            """
            try:
                return await asyncio.wait_for(
                    task, timeout=request.limits.output_drain_join_s
                )
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return None

        # Guest-caused output flood is scientific OUTPUT_EXCEEDED even when the
        # deadline also tripped and even if a drain is stuck (sandbox.py parity:
        # overflow before timeout / before drain-stuck).
        if out_overflow[0] | err_overflow[0]:
            exit_code = await _reap_guest()
            stdout = await _drain_or_none(out_task)
            stderr = await _drain_or_none(err_task)
            result = _to_result(
                SandboxStatus.OUTPUT_EXCEEDED,
                exit_code=exit_code,
                stdout=_or_empty(stdout),
                stderr=_or_empty(stderr),
                reason="output_exceeded",
                duration_s=asyncio.get_running_loop().time() - started,
                drain_stuck=stdout is None or stderr is None,
            )
            outcome = sandbox_status_to_coder(result, job_id=jid, attempt_id=request.attempt_id)
            _ensure_terminal(jobs, jid, detail="output_exceeded")
            return outcome

        job = jobs.get(jid)
        if wait_task in done and not wait_task.cancelled():
            job = wait_task.result()
        if job.status == "timed_out":
            # Job deadline tripped while the guest still runs.
            await _reap_guest()
            out_task.cancel()
            err_task.cancel()
            await asyncio.gather(out_task, err_task, return_exceptions=True)
            return CoderOutcome(
                job_id=jid,
                attempt_id=request.attempt_id,
                status="timed_out",
                retryable=True,
                artifact_ids=[],
                detail="job_deadline",
            )

        # Guest exited on its own; drain capped readers (bounded — a
        # grandchild may still hold the write end open).
        stdout = await _drain_or_none(out_task)
        stderr = await _drain_or_none(err_task)
        duration_s = asyncio.get_running_loop().time() - started
        exit_code = proc.returncode

        if stdout is None or stderr is None:
            # Incomplete sinks — infra, not scientific (sandbox.py parity).
            result = _to_result(
                SandboxStatus.UNAVAILABLE,
                exit_code=exit_code,
                stdout=_or_empty(stdout),
                stderr=_or_empty(stderr),
                reason="sandbox_unavailable:output_drain",
                duration_s=duration_s,
                drain_stuck=True,
            )
            outcome = sandbox_status_to_coder(result, job_id=jid, attempt_id=request.attempt_id)
            jobs.complete(jid, status="failed", artifacts=[])
            return outcome

        if exit_code != 0:
            result = _to_result(
                SandboxStatus.NONZERO,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                reason=f"exit:{exit_code}",
                duration_s=duration_s,
            )
            outcome = sandbox_status_to_coder(result, job_id=jid, attempt_id=request.attempt_id)
            jobs.complete(jid, status="failed", artifacts=[])
            return outcome

        result = _to_result(
            SandboxStatus.OK,
            exit_code=0,
            stdout=stdout,
            stderr=stderr,
            reason="ok",
            duration_s=duration_s,
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
    finally:
        if proc is not None:
            _close_pipes(proc)
        shutil.rmtree(workspace, ignore_errors=True)
