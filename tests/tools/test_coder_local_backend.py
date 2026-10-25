"""local-python coder backend: selection refuse + run semantics (macOS-runnable)."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coscientist.core.events.clock import SystemClock
from coscientist.core.events.events import UnstampedEvent
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.tools.coder import (
    CoderOutputSpec,
    CoderRequest,
    require_local_python,
    resolve_coder_backend,
    run_local_coder_job,
)
from coscientist.tools.contract import ToolError
from coscientist.tools.jobs import InMemoryJobStore
from tests.checks.sandbox_test_limits import sandbox_limits


class _Journal:
    def __init__(self) -> None:
        self.events: list[UnstampedEvent] = []

    def record(self, event: UnstampedEvent) -> None:
        self.events.append(event)


def _jobs() -> InMemoryJobStore:
    return InMemoryJobStore(
        study_id="st1",
        clock=SystemClock(),
        journal=_Journal(),
        on_clock_change=None,
    )


def _request(
    code: str,
    *,
    timeout_s: float = 10.0,
    max_output_bytes: int = 65536,
    allow_network: bool = True,
    inputs: dict[str, bytes] | None = None,
    env: dict[str, str] | None = None,
) -> CoderRequest:
    return CoderRequest(
        attempt_id="att1",
        call_id="call1",
        argv=(sys.executable, "-c", code),
        inputs=inputs or {},
        env=env or {},
        limits=sandbox_limits(
            timeout_s=timeout_s,
            memory_mb=64,
            max_output_bytes=max_output_bytes,
            allow_network=allow_network,
            allow_cgroup_degraded=False,
        ),
        deadline=datetime.now(UTC) + timedelta(seconds=60),
        outputs=(
            CoderOutputSpec(name="stdout.txt", source="stdout", media_type="text/plain"),
        ),
        attempt_is_closed=lambda: False,
    )


# --- selection: explicit config only -----------------------------------------


@pytest.mark.parametrize("missing", [None, "", "auto", "bwrap-local", 1, True])
def test_backend_refuses_without_explicit_config(missing: object) -> None:
    with pytest.raises(ToolError, match="explicit backend required") as ei:
        resolve_coder_backend(missing)
    assert ei.value.kind == "local"


def test_backend_resolves_exact_values() -> None:
    assert resolve_coder_backend("bwrap") == "bwrap"
    assert resolve_coder_backend("local-python") == "local-python"


def test_local_python_refuses_network_block_request() -> None:
    with pytest.raises(ToolError, match="network is NOT isolated") as ei:
        require_local_python(_request("pass", allow_network=False))
    assert ei.value.kind == "local"


def test_local_python_refuses_degraded_bypass() -> None:
    request = CoderRequest(
        attempt_id="att1",
        call_id="call1",
        argv=(sys.executable, "-c", "pass"),
        inputs={},
        env={},
        limits=sandbox_limits(
            timeout_s=5.0,
            memory_mb=64,
            max_output_bytes=4096,
            allow_network=True,
            allow_cgroup_degraded=True,
        ),
        deadline=datetime.now(UTC) + timedelta(seconds=60),
        outputs=(),
        attempt_is_closed=lambda: False,
    )
    with pytest.raises(ToolError, match="allow_cgroup_degraded bypass forbidden"):
        require_local_python(request)


# --- run semantics ------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_run_succeeds(tmp_path: Path) -> None:
    arts = LocalArtifactStore(tmp_path / "arts")
    outcome = await run_local_coder_job(
        _request("print('hello-local')"),
        jobs=_jobs(),
        artifacts=arts,
        workspace_root=tmp_path,
    )
    assert outcome.status == "succeeded"
    assert outcome.retryable is False
    assert len(outcome.artifact_ids) == 1
    meta = arts.get_meta(outcome.artifact_ids[0])
    assert meta is not None and meta.net_isolation == "none"


@pytest.mark.asyncio
async def test_local_run_reads_inputs_cwd_isolated(tmp_path: Path) -> None:
    arts = LocalArtifactStore(tmp_path / "arts")
    outcome = await run_local_coder_job(
        _request(
            "from pathlib import Path; print(sorted(p.name for p in Path('.').iterdir()))",
            inputs={"data.csv": b"a,b\n1,2\n"},
        ),
        jobs=_jobs(),
        artifacts=arts,
        workspace_root=tmp_path,
    )
    assert outcome.status == "succeeded"
    art = arts.get_meta(outcome.artifact_ids[0])
    assert art is not None
    assert b"data.csv" in arts.read(outcome.artifact_ids[0])


@pytest.mark.asyncio
async def test_local_run_nonzero_exit(tmp_path: Path) -> None:
    outcome = await run_local_coder_job(
        _request("import sys; sys.exit(3)"),
        jobs=_jobs(),
        artifacts=LocalArtifactStore(tmp_path / "arts"),
        workspace_root=tmp_path,
    )
    assert outcome.status == "failed"
    assert outcome.retryable is False
    assert "exit:3" in outcome.detail


@pytest.mark.asyncio
async def test_local_run_timeout_kill(tmp_path: Path) -> None:
    outcome = await run_local_coder_job(
        _request(
            "import os, time; open(os.environ['PIDFILE'],'w').write(str(os.getpid()));"
            " time.sleep(30)",
            timeout_s=0.5,
            env={"PIDFILE": str(tmp_path / "guest.pid")},
        ),
        jobs=_jobs(),
        artifacts=LocalArtifactStore(tmp_path / "arts"),
        workspace_root=tmp_path,
    )
    assert outcome.status == "timed_out"
    assert outcome.retryable is True
    assert outcome.detail == "job_deadline"
    # Guest process group actually died (pidfile written before the sleep).
    pid = int((tmp_path / "guest.pid").read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError(f"guest pid {pid} still alive after timeout kill")


@pytest.mark.asyncio
async def test_local_run_flooding_guest_is_output_exceeded(tmp_path: Path) -> None:
    """Flooding guest that never exits: scientific OUTPUT_EXCEEDED, not timed_out."""
    outcome = await run_local_coder_job(
        _request("while True: print('x' * 100)", timeout_s=30.0, max_output_bytes=4096),
        jobs=_jobs(),
        artifacts=LocalArtifactStore(tmp_path / "arts"),
        workspace_root=tmp_path,
    )
    assert outcome.status == "failed"
    assert outcome.retryable is False
    assert "output_exceeded" in outcome.detail


@pytest.mark.asyncio
async def test_local_run_output_cap(tmp_path: Path) -> None:
    outcome = await run_local_coder_job(
        _request("print('x' * 100000)", max_output_bytes=4096),
        jobs=_jobs(),
        artifacts=LocalArtifactStore(tmp_path / "arts"),
        workspace_root=tmp_path,
    )
    assert outcome.status == "failed"
    assert outcome.retryable is False
    assert "output_exceeded" in outcome.detail
