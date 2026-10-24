"""C1 coder job + coder_timeout R4 (DESIGN §6.4 / §12.2 / CAP #11)."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing as mp
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from coscientist.checks.sandbox import SandboxResult, SandboxStatus
from coscientist.core.events.clock import FakeClock, SystemClock
from coscientist.core.events.errors import ArtifactRejected
from coscientist.core.events.events import UnstampedEvent
from coscientist.core.events.job_events import job_is_live
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.tools.coder import (
    CoderOutcome,
    CoderOutputSpec,
    CoderRequest,
    run_coder_job,
    sandbox_status_to_coder,
)
from coscientist.tools.contract import ToolError
from coscientist.tools.jobs import InMemoryJobStore
from tests.checks.sandbox_test_limits import sandbox_limits


class _Journal:
    def __init__(self) -> None:
        self.events: list[UnstampedEvent] = []

    def record(self, event: UnstampedEvent) -> None:
        self.events.append(event)


def _types(journal: _Journal) -> list[str]:
    return [e.type for e in journal.events]


def _job_id(journal: _Journal) -> str:
    for event in journal.events:
        if event.type == "JobSubmitted":
            return str(event.payload["job_id"])
    raise AssertionError("no JobSubmitted")


def _jobs(clock: FakeClock | SystemClock, journal: _Journal) -> InMemoryJobStore:
    return InMemoryJobStore(study_id="st1", clock=clock, journal=journal)


def _fake_ok(*, stdout: bytes = b"hello\n") -> SandboxResult:
    return SandboxResult(
        status=SandboxStatus.OK,
        exit_code=0,
        stdout=stdout,
        stderr=b"",
        reason="ok",
        duration_s=0.01,
        cgroup_limits_held=True,
        net_isolation="hard",
        fs_isolation="hard",
        oom_detectable=True,
    )


def _fake_status(status: SandboxStatus, *, reason: str = "r") -> SandboxResult:
    return SandboxResult(
        status=status,
        exit_code=1 if status != SandboxStatus.OK else 0,
        stdout=b"",
        stderr=b"",
        reason=reason,
        duration_s=0.01,
        cgroup_limits_held=True,
        net_isolation="hard",
        fs_isolation="hard",
        oom_detectable=True,
    )


def _open() -> bool:
    return False


@pytest.mark.asyncio
async def test_coder_success_registers_sha256_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")

    def _worker(
        conn: Connection,
        _argv: list[str],
        _limits: object,
        _inputs: dict[str, bytes],
        _env: dict[str, str],
    ) -> None:
        conn.send(("ok", _fake_ok(stdout=b"payload-bytes")))
        conn.close()

    monkeypatch.setattr("coscientist.tools.coder.run._sandbox_worker", _worker)
    out = await run_coder_job(
        CoderRequest(
            attempt_id="att#1",
            call_id="call_1",
            argv=("/bin/echo", "x"),
            inputs={},
            env={},
            limits=sandbox_limits(timeout_s=5.0, memory_mb=64, max_output_bytes=4096),
            deadline=clock.now() + timedelta(seconds=30),
            outputs=(CoderOutputSpec(name="out.txt", source="stdout", media_type="text/plain"),),
            attempt_is_closed=_open,
        ),
        jobs=jobs,
        artifacts=arts,
    )
    assert out.status == "succeeded"
    assert out.retryable is False
    assert len(out.artifact_ids) == 1
    meta = arts.get_meta(out.artifact_ids[0])
    assert meta is not None
    assert meta.sha256 == hashlib.sha256(b"payload-bytes").hexdigest()
    assert meta.producer == "sandbox"
    assert jobs.get(out.job_id).status == "succeeded"


@pytest.mark.asyncio
async def test_coder_timeout_kills_guest_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deadline → timed_out + guest process not alive (DESIGN §6.4)."""
    import coscientist.tools.coder.run as coder_run

    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")
    marker = tmp_path / "started"
    killed: list[mp.process.BaseProcess] = []
    real_kill = coder_run._kill_sandbox_process

    def _hang_worker(
        conn: Connection,
        _argv: list[str],
        _limits: object,
        _inputs: dict[str, bytes],
        _env: dict[str, str],
    ) -> None:
        marker.write_text("1", encoding="utf-8")
        import time

        time.sleep(60)
        conn.send(("ok", _fake_ok()))
        conn.close()

    async def _spy_kill(
        proc: mp.process.BaseProcess,
        *,
        reap_timeout_s: float,
        guest_done: asyncio.Event,
    ) -> bool:
        killed.append(proc)
        return await real_kill(proc, reap_timeout_s=reap_timeout_s, guest_done=guest_done)

    monkeypatch.setattr(coder_run, "_sandbox_worker", _hang_worker)
    monkeypatch.setattr(coder_run, "_kill_sandbox_process", _spy_kill)

    deadline = clock.now() + timedelta(seconds=2)

    async def _advance() -> None:
        while not marker.exists():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        clock.advance(timedelta(seconds=3))

    adv = asyncio.create_task(_advance())
    out = await run_coder_job(
        CoderRequest(
            attempt_id="att#to",
            call_id="call_to",
            argv=("sleep", "60"),
            inputs={},
            env={},
            limits=sandbox_limits(timeout_s=60.0, memory_mb=64, max_output_bytes=4096),
            deadline=deadline,
            outputs=(),
            attempt_is_closed=_open,
        ),
        jobs=jobs,
        artifacts=arts,
    )
    await adv
    assert out.status == "timed_out"
    assert out.retryable is True
    assert jobs.get(out.job_id).status == "timed_out"
    assert "JobTimedOut" in _types(journal)
    assert "JobCancelled" in _types(journal)
    assert killed, "kill was not invoked"
    assert not killed[0].is_alive()


@pytest.mark.asyncio
async def test_coder_timeout_r4_wall_clock(tmp_path: Path) -> None:
    from coscientist.checks.sandbox_probe import isolation_fully_hard, resolve_isolation

    net, fs = resolve_isolation(allow_network=False)
    if not isolation_fully_hard(net, fs):
        pytest.skip("hard bwrap isolation required")

    clock = SystemClock()
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")
    deadline = clock.now() + timedelta(seconds=1)
    t0 = clock.now()
    out = await run_coder_job(
        CoderRequest(
            attempt_id="att#wall",
            call_id="call_wall",
            argv=("python3", "-c", "import time; time.sleep(5)"),
            inputs={},
            env={},
            limits=sandbox_limits(
                timeout_s=3.0,
                memory_mb=64,
                max_output_bytes=4096,
                allow_cgroup_degraded=True,
            ),
            deadline=deadline,
            outputs=(),
            attempt_is_closed=_open,
        ),
        jobs=jobs,
        artifacts=arts,
    )
    done = clock.now()
    assert out.status == "timed_out"
    assert out.retryable is True
    assert (done - t0) <= timedelta(seconds=1, milliseconds=800)
    assert "JobTimedOut" in _types(journal)


@pytest.mark.asyncio
async def test_coder_refuses_late_artifacts_via_attempt_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")

    def _worker(
        conn: Connection,
        _argv: list[str],
        _limits: object,
        _inputs: dict[str, bytes],
        _env: dict[str, str],
    ) -> None:
        conn.send(("ok", _fake_ok(stdout=b"late")))
        conn.close()

    monkeypatch.setattr("coscientist.tools.coder.run._sandbox_worker", _worker)
    # Put-time check: attempt closed while guest ran (callable, not frozen bool).
    with pytest.raises(ArtifactRejected, match="attempt_closed"):
        await run_coder_job(
            CoderRequest(
                attempt_id="att#closed",
                call_id="call_c",
                argv=("/bin/true",),
                inputs={},
                env={},
                limits=sandbox_limits(timeout_s=5.0, memory_mb=64, max_output_bytes=4096),
                deadline=clock.now() + timedelta(seconds=30),
                outputs=(CoderOutputSpec(name="x", source="stdout", media_type="text/plain"),),
                attempt_is_closed=lambda: True,
            ),
            jobs=jobs,
            artifacts=arts,
        )
    assert not job_is_live(jobs.get(_job_id(journal)).status)


@pytest.mark.asyncio
async def test_coder_exception_leaves_job_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")

    def _boom(
        conn: Connection,
        _argv: list[str],
        _limits: object,
        _inputs: dict[str, bytes],
        _env: dict[str, str],
    ) -> None:
        conn.send(("err", RuntimeError("sandbox boom")))
        conn.close()

    monkeypatch.setattr("coscientist.tools.coder.run._sandbox_worker", _boom)
    with pytest.raises(RuntimeError, match="sandbox boom"):
        await run_coder_job(
            CoderRequest(
                attempt_id="att#boom",
                call_id="call_b",
                argv=("/bin/true",),
                inputs={},
                env={},
                limits=sandbox_limits(timeout_s=5.0, memory_mb=64, max_output_bytes=4096),
                deadline=clock.now() + timedelta(seconds=30),
                outputs=(),
                attempt_is_closed=_open,
            ),
            jobs=jobs,
            artifacts=arts,
        )
    job = jobs.get(_job_id(journal))
    assert job.status == "failed"
    assert not job_is_live(job.status)


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (SandboxStatus.TIMEOUT, True),
        (SandboxStatus.UNAVAILABLE, False),
        (SandboxStatus.BLOCKED, False),
        (SandboxStatus.OOM, False),
        (SandboxStatus.NONZERO, False),
        (SandboxStatus.OUTPUT_EXCEEDED, False),
    ],
)
def test_sandbox_status_mapping(status: SandboxStatus, retryable: bool) -> None:
    out = sandbox_status_to_coder(
        _fake_status(status, reason="detail"),
        job_id="job_x",
        attempt_id="att",
    )
    assert out == CoderOutcome(
        job_id="job_x",
        attempt_id="att",
        status="failed",
        retryable=retryable,
        artifact_ids=[],
        detail=f"sandbox:{status.value}:detail",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (SandboxStatus.TIMEOUT, True),
        (SandboxStatus.UNAVAILABLE, False),
        (SandboxStatus.NONZERO, False),
    ],
)
async def test_coder_job_maps_sandbox_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: SandboxStatus,
    retryable: bool,
) -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")

    def _worker(
        conn: Connection,
        _argv: list[str],
        _limits: object,
        _inputs: dict[str, bytes],
        _env: dict[str, str],
    ) -> None:
        conn.send(("ok", _fake_status(status, reason="mapped")))
        conn.close()

    monkeypatch.setattr("coscientist.tools.coder.run._sandbox_worker", _worker)
    out = await run_coder_job(
        CoderRequest(
            attempt_id=f"att#{status.value}",
            call_id="call_m",
            argv=("/bin/false",),
            inputs={},
            env={},
            limits=sandbox_limits(timeout_s=5.0, memory_mb=64, max_output_bytes=4096),
            deadline=clock.now() + timedelta(seconds=30),
            outputs=(),
            attempt_is_closed=_open,
        ),
        jobs=jobs,
        artifacts=arts,
    )
    assert out.status == "failed"
    assert out.retryable is retryable
    assert jobs.get(out.job_id).status == "failed"


def test_coder_request_refuses_naive_deadline() -> None:
    with pytest.raises(ToolError, match="timezone-aware"):
        CoderRequest(
            attempt_id="a",
            call_id="c",
            argv=("/bin/true",),
            inputs={},
            env={},
            limits=sandbox_limits(timeout_s=5.0, memory_mb=64, max_output_bytes=1024),
            deadline=datetime(2026, 10, 24),
            outputs=(),
            attempt_is_closed=_open,
        )
