"""C1 coder job + coder_timeout R4 (DESIGN §6.4 / §12.2 / CAP #11)."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coscientist.checks.sandbox import SandboxResult, SandboxStatus
from coscientist.core.events.clock import FakeClock, SystemClock
from coscientist.core.events.events import UnstampedEvent
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.tools.coder import (
    CoderOutcome,
    CoderOutputSpec,
    CoderRequest,
    run_coder_job,
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


@pytest.mark.asyncio
async def test_coder_success_registers_sha256_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")

    def _run(*_a: object, **_k: object) -> SandboxResult:
        return _fake_ok(stdout=b"payload-bytes")

    monkeypatch.setattr("coscientist.tools.coder.run.run_sandbox", _run)
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
            attempt_closed=False,
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
    assert "JobSubmitted" in _types(journal)
    assert "JobStatusChanged" in _types(journal)


@pytest.mark.asyncio
async def test_coder_timeout_r4_fake_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Job past deadline → JobTimedOut + failure(retryable) without waiting forever."""
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")
    started = threading.Event()
    release = threading.Event()

    def _hang(*_a: object, **_k: object) -> SandboxResult:
        started.set()
        release.wait(timeout=30.0)
        return _fake_ok()

    monkeypatch.setattr("coscientist.tools.coder.run.run_sandbox", _hang)
    deadline = clock.now() + timedelta(seconds=2)

    async def _advance() -> None:
        while not started.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        clock.advance(timedelta(seconds=3))

    adv = asyncio.create_task(_advance())
    t0 = time.perf_counter()
    out = await run_coder_job(
        CoderRequest(
            attempt_id="att#to",
            call_id="call_to",
            argv=("sleep", "30"),
            inputs={},
            env={},
            limits=sandbox_limits(timeout_s=60.0, memory_mb=64, max_output_bytes=4096),
            deadline=deadline,
            outputs=(),
            attempt_closed=False,
        ),
        jobs=jobs,
        artifacts=arts,
    )
    await adv
    release.set()
    elapsed = time.perf_counter() - t0
    assert out == CoderOutcome(
        job_id=out.job_id,
        attempt_id="att#to",
        status="timed_out",
        retryable=True,
        artifact_ids=[],
        detail="job_deadline",
    )
    assert jobs.get(out.job_id).status == "timed_out"
    assert _types(journal)[:3] == ["JobSubmitted", "JobStatusChanged", "JobTimedOut"]
    assert "JobCancelled" in _types(journal)
    # Wall time for the runner must not track the hung sandbox (≤ deadline+tick).
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_coder_timeout_r4_wall_clock(tmp_path: Path) -> None:
    """Live bwrap sleep past job deadline → timed_out retryable ≤ deadline+tick."""
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
            attempt_closed=False,
        ),
        jobs=jobs,
        artifacts=arts,
    )
    done = clock.now()
    assert out.status == "timed_out"
    assert out.retryable is True
    assert out.detail == "job_deadline"
    assert (done - t0) <= timedelta(seconds=1, milliseconds=800)
    assert "JobTimedOut" in _types(journal)
    assert "JobCancelled" in _types(journal)


@pytest.mark.asyncio
async def test_coder_refuses_artifacts_when_attempt_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coscientist.core.events.errors import ArtifactRejected

    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")
    monkeypatch.setattr(
        "coscientist.tools.coder.run.run_sandbox",
        lambda *_a, **_k: _fake_ok(stdout=b"late"),
    )
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
                attempt_closed=True,
            ),
            jobs=jobs,
            artifacts=arts,
        )


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
            attempt_closed=False,
        )
