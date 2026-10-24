"""C1 coder job + coder_timeout R4 (DESIGN §6.4 / §12.2 / CAP #11)."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coscientist.checks.sandbox import SandboxLimits, SandboxResult, SandboxStatus
from coscientist.checks.sandbox_probe import isolation_fully_hard, resolve_isolation
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
from coscientist.tools.coder import run as coder_run
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


def _open() -> bool:
    return False


def _require_hard_bwrap() -> None:
    net, fs = resolve_isolation(allow_network=False)
    if not isolation_fully_hard(net, fs):
        pytest.skip("hard bwrap isolation required")


def _limits(
    *,
    timeout_s: float = 5.0,
    memory_mb: int = 64,
    max_output_bytes: int = 4096,
    **overrides: object,
) -> SandboxLimits:
    return sandbox_limits(
        timeout_s=timeout_s,
        memory_mb=memory_mb,
        max_output_bytes=max_output_bytes,
        allow_cgroup_degraded=True,
        **overrides,
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


@pytest.mark.asyncio
async def test_coder_success_registers_sha256_artifact(tmp_path: Path) -> None:
    _require_hard_bwrap()
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")
    out = await run_coder_job(
        CoderRequest(
            attempt_id="att#1",
            call_id="call_1",
            argv=("/bin/echo", "payload-bytes"),
            inputs={},
            env={},
            limits=_limits(),
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
    assert meta.sha256 == hashlib.sha256(b"payload-bytes\n").hexdigest()
    assert meta.producer == "sandbox"
    assert jobs.get(out.job_id).status == "succeeded"


@pytest.mark.asyncio
async def test_coder_timeout_kills_guest_process_group(tmp_path: Path) -> None:
    """Deadline → timed_out + real guest process group gone via killpg."""
    _require_hard_bwrap()
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")
    token = uuid.uuid4().hex
    guest_script = f"import time;time.sleep(60)  # coder-guest-{token}"

    seen_pgid: list[int] = []
    real_kill = coder_run._kill_sandbox_guest

    async def _spy_kill(
        proc: object,
        *,
        guest_pgid: int,
        reap_timeout_s: float,
        guest_done: asyncio.Event,
    ) -> bool:
        seen_pgid.append(guest_pgid)
        return await real_kill(
            proc,  # type: ignore[arg-type]
            guest_pgid=guest_pgid,
            reap_timeout_s=reap_timeout_s,
            guest_done=guest_done,
        )

    coder_run._kill_sandbox_guest = _spy_kill  # type: ignore[assignment]
    deadline = clock.now() + timedelta(seconds=2)

    async def _advance() -> None:
        # Spawn + bwrap guest must start before FakeClock crosses deadline.
        await asyncio.sleep(1.0)
        clock.advance(timedelta(seconds=3))

    try:
        adv = asyncio.create_task(_advance())
        out = await run_coder_job(
            CoderRequest(
                attempt_id="att#to",
                call_id="call_to",
                argv=("python3", "-c", guest_script),
                inputs={},
                env={},
                limits=_limits(timeout_s=60.0),
                deadline=deadline,
                outputs=(),
                attempt_is_closed=_open,
            ),
            jobs=jobs,
            artifacts=arts,
        )
        await adv
    finally:
        coder_run._kill_sandbox_guest = real_kill  # type: ignore[assignment]

    assert out.status == "timed_out"
    assert out.retryable is True
    assert jobs.get(out.job_id).status == "timed_out"
    assert "JobTimedOut" in _types(journal)
    assert "JobCancelled" in _types(journal)
    assert seen_pgid and seen_pgid[0] > 0, "guest pgid was never captured"
    assert not coder_run._pg_alive(seen_pgid[0]), "guest process group still alive after killpg"


@pytest.mark.asyncio
async def test_coder_timeout_r4_wall_clock(tmp_path: Path) -> None:
    _require_hard_bwrap()
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
            limits=_limits(timeout_s=3.0),
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
async def test_coder_refuses_late_artifacts_via_attempt_is_closed(tmp_path: Path) -> None:
    _require_hard_bwrap()
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")
    with pytest.raises(ArtifactRejected, match="attempt_closed"):
        await run_coder_job(
            CoderRequest(
                attempt_id="att#closed",
                call_id="call_c",
                argv=("/bin/echo", "late"),
                inputs={},
                env={},
                limits=_limits(),
                deadline=clock.now() + timedelta(seconds=30),
                outputs=(CoderOutputSpec(name="x", source="stdout", media_type="text/plain"),),
                attempt_is_closed=lambda: True,
            ),
            jobs=jobs,
            artifacts=arts,
        )
    assert not job_is_live(jobs.get(_job_id(journal)).status)


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
async def test_coder_job_maps_nonzero_sandbox_status(tmp_path: Path) -> None:
    _require_hard_bwrap()
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")
    out = await run_coder_job(
        CoderRequest(
            attempt_id="att#nonzero",
            call_id="call_m",
            argv=("/bin/false",),
            inputs={},
            env={},
            limits=_limits(),
            deadline=clock.now() + timedelta(seconds=30),
            outputs=(),
            attempt_is_closed=_open,
        ),
        jobs=jobs,
        artifacts=arts,
    )
    assert out.status == "failed"
    assert out.retryable is False
    assert jobs.get(out.job_id).status == "failed"
    assert out.detail.startswith("sandbox:nonzero:")


def test_coder_request_refuses_naive_deadline() -> None:
    with pytest.raises(ToolError, match="timezone-aware"):
        CoderRequest(
            attempt_id="a",
            call_id="c",
            argv=("/bin/true",),
            inputs={},
            env={},
            limits=_limits(timeout_s=5.0, max_output_bytes=1024),
            deadline=datetime(2026, 10, 24),
            outputs=(),
            attempt_is_closed=_open,
        )


def test_ensure_terminal_completes_live_job() -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    jid = jobs.submit("coder", {"x": 1}, "att#term", clock.now() + timedelta(seconds=30))
    jobs.mark_running(jid)
    coder_run._ensure_terminal(jobs, jid, detail="test")
    assert jobs.get(jid).status == "failed"
    assert not job_is_live(jobs.get(jid).status)
