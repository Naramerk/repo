"""C1 coder job + coder_timeout R4 (DESIGN §6.4 / §12.2 / CAP #11)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import uuid
from datetime import UTC, datetime, timedelta
from multiprocessing.process import BaseProcess
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
    on_change = clock.on_change if isinstance(clock, FakeClock) else None
    return InMemoryJobStore(
        study_id="st1",
        clock=clock,
        journal=journal,
        on_clock_change=on_change,
    )


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


def _silent_sandbox_worker(
    conn: object,
    argv: list[str],
    limits: SandboxLimits,
    inputs: dict[str, bytes],
    env: dict[str, str],
    guest_pgid: object,
) -> None:
    """Spawn-picklable worker that never publishes pgid (fail-closed test)."""
    import time

    time.sleep(60)


def _worker_publish_pgid_no_result(
    conn: object,
    argv: list[str],
    limits: SandboxLimits,
    inputs: dict[str, bytes],
    env: dict[str, str],
    guest_pgid: coder_run.GuestPgidSlot,
) -> None:
    """Publish a fake pgid then exit without a result (finally-cleanup test)."""
    with guest_pgid.get_lock():
        guest_pgid.value = 4242


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
    jobs.close()


@pytest.mark.asyncio
async def test_coder_timeout_kills_guest_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        jobs: InMemoryJobStore,
        jid: str,
        proc: BaseProcess,
        *,
        guest_pgid: int,
        term_grace_s: float,
        reap_timeout_s: float,
        guest_done: asyncio.Event,
    ) -> bool:
        seen_pgid.append(guest_pgid)
        return await real_kill(
            jobs,
            jid,
            proc,
            guest_pgid=guest_pgid,
            term_grace_s=term_grace_s,
            reap_timeout_s=reap_timeout_s,
            guest_done=guest_done,
        )

    monkeypatch.setattr(coder_run, "_kill_sandbox_guest", _spy_kill)
    deadline = clock.now() + timedelta(seconds=2)

    async def _advance() -> None:
        # Spawn + bwrap guest must start before FakeClock crosses deadline.
        await asyncio.sleep(1.0)
        clock.advance(timedelta(seconds=3))

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

    assert out.status == "timed_out"
    assert out.retryable is True
    assert jobs.get(out.job_id).status == "timed_out"
    assert "JobTimedOut" in _types(journal)
    assert "JobCancelled" in _types(journal)
    assert seen_pgid and seen_pgid[0] > 0, "guest pgid was never captured"
    with pytest.raises(ProcessLookupError):
        os.killpg(seen_pgid[0], 0)
    assert "JobGuestProcessGroupGone" in _types(journal)
    jobs.close()


@pytest.mark.asyncio
async def test_coder_timeout_unpublished_pgid_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If sandbox never publishes pgid, cancel-failed is journaled (not guest-gone)."""
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")

    monkeypatch.setattr(coder_run, "_sandbox_worker", _silent_sandbox_worker)
    deadline = clock.now() + timedelta(seconds=1)

    async def _advance() -> None:
        await asyncio.sleep(0)
        clock.advance(timedelta(seconds=2))

    adv = asyncio.create_task(_advance())
    out = await run_coder_job(
        CoderRequest(
            attempt_id="att#nopgid",
            call_id="call_nopgid",
            argv=("/bin/true",),
            inputs={},
            env={},
            limits=_limits(timeout_s=60.0, reap_timeout_s=0.05),
            deadline=deadline,
            outputs=(),
            attempt_is_closed=_open,
        ),
        jobs=jobs,
        artifacts=arts,
    )
    await adv
    assert out.status == "timed_out"
    assert "JobCancelFailed" in _types(journal)
    reasons = [e.payload["reason"] for e in journal.events if e.type == "JobCancelFailed"]
    assert "guest_pgid_unpublished" in reasons
    jobs.close()


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
    jobs.close()


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
    jobs.close()


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
    ids=["timeout", "unavailable", "blocked", "oom", "nonzero", "output_exceeded"],
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
    jobs.close()


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
    jobs.close()


@pytest.mark.parametrize(
    "bad",
    [-1.0, float("nan")],
    ids=["negative", "nan"],
)
def test_await_guest_pgid_refuses_bad_timeout(bad: float) -> None:
    with pytest.raises(ToolError, match="guest_pgid_timeout_s"):
        asyncio.run(
            coder_run._await_guest_pgid(
                type("S", (), {"value": 0})(),
                timeout_s=bad,
                poll_interval_s=0.01,
            )
        )


@pytest.mark.asyncio
async def test_kill_sandbox_guest_unpublished_pgid_returns_false() -> None:
    class _DeadProc:
        def is_alive(self) -> bool:
            return False

        def terminate(self) -> None:
            raise AssertionError("terminate must not run when already dead")

        def kill(self) -> None:
            raise AssertionError("kill must not run when already dead")

    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    jid = jobs.submit(
        "coder",
        {"call_id": "dead"},
        "att#dead",
        clock.now() + timedelta(seconds=30),
    )
    jobs.mark_running(jid)
    done = asyncio.Event()
    done.set()
    killed = await coder_run._kill_sandbox_guest(
        jobs,
        jid,
        _DeadProc(),
        guest_pgid=0,
        term_grace_s=0.01,
        reap_timeout_s=0.01,
        guest_done=done,
    )
    assert killed is False
    assert "JobGuestProcessGroupGone" not in _types(journal)
    jobs.close()


@pytest.mark.parametrize(
    "bad",
    [0, -1, True],
    ids=["zero", "negative", "bool"],
)
def test_set_guest_pgid_refuses_non_positive(bad: object) -> None:
    class _Slot:
        value = 0

        def get_lock(self) -> object:
            raise AssertionError("lock must not run on refuse")

    with pytest.raises(ToolError, match="positive int"):
        coder_run._set_guest_pgid(_Slot(), bad)


def _running_job() -> tuple[InMemoryJobStore, _Journal, str]:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    jid = jobs.submit(
        "coder",
        {"call_id": "pg"},
        "att#pg",
        clock.now() + timedelta(seconds=30),
    )
    jobs.mark_running(jid)
    return jobs, journal, jid


def test_signal_guest_pg_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "killpg", lambda _pgid, _sig: None)
    coder_run._signal_guest_pg(4242, signal.SIGTERM)


@pytest.mark.parametrize(
    "bad",
    [0, -1],
    ids=["zero", "negative"],
)
def test_signal_guest_pg_refuses_non_positive(bad: int) -> None:
    with pytest.raises(ToolError, match="signal pgid must be positive"):
        coder_run._signal_guest_pg(bad, signal.SIGTERM)


def test_signal_guest_pg_refuses_bool() -> None:
    with pytest.raises(ToolError, match="signal pgid must be positive"):
        coder_run._signal_guest_pg(True, signal.SIGTERM)


def test_signal_guest_pg_esrch_raises_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _gone(_pgid: int, _sig: int) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr(os, "killpg", _gone)
    with pytest.raises(coder_run.GuestProcessGroupGone, match="pgid=4242.*SIGTERM"):
        coder_run._signal_guest_pg(4242, signal.SIGTERM)


@pytest.mark.parametrize(
    "bad",
    [0, -1],
    ids=["zero", "negative"],
)
def test_guest_process_group_gone_refuses_non_positive(bad: int) -> None:
    with pytest.raises(ToolError, match="GuestProcessGroupGone pgid must be positive"):
        coder_run.GuestProcessGroupGone(pgid=bad, op="probe", sig_name="0")


def test_guest_process_group_gone_refuses_bool() -> None:
    with pytest.raises(ToolError, match="GuestProcessGroupGone pgid must be positive"):
        coder_run.GuestProcessGroupGone(pgid=True, op="probe", sig_name="0")


@pytest.mark.parametrize(
    "bad",
    [0, -1],
    ids=["zero", "negative"],
)
def test_guest_pg_state_refuses_non_positive(bad: int) -> None:
    with pytest.raises(ToolError, match="signal pgid must be positive"):
        coder_run._guest_pg_state(bad)


def test_guest_pg_state_gone_without_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "isdir", lambda _path: False)
    assert coder_run._guest_pg_state(4242) == "gone"


def test_guest_pg_state_alive_with_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "isdir", lambda _path: True)
    assert coder_run._guest_pg_state(4242) == "alive"


def test_signal_or_record_gone_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, journal, jid = _running_job()
    monkeypatch.setattr(coder_run, "_guest_pg_state", lambda _pgid: "alive")
    monkeypatch.setattr(os, "killpg", lambda _pgid, _sig: None)
    assert coder_run._signal_or_record_gone(jobs, jid, pgid=4242, sig=signal.SIGTERM) == "ok"
    assert "JobGuestProcessGroupGone" not in _types(journal)
    jobs.close()


def test_signal_or_record_gone_journals_killpg_when_proc_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, journal, jid = _running_job()
    monkeypatch.setattr(coder_run, "_guest_pg_state", lambda _pgid: "gone")

    def _must_not_kill(_pgid: int, _sig: int) -> None:
        raise AssertionError("killpg must not run when /proc says gone")

    monkeypatch.setattr(os, "killpg", _must_not_kill)
    assert coder_run._signal_or_record_gone(jobs, jid, pgid=4242, sig=signal.SIGKILL) == "gone"
    gone_events = [e for e in journal.events if e.type == "JobGuestProcessGroupGone"]
    assert len(gone_events) == 1
    assert gone_events[0].payload["pgid"] == 4242
    assert gone_events[0].payload["op"] == "killpg"
    assert gone_events[0].payload["sig_name"] == "SIGKILL"
    jobs.close()


def test_signal_or_record_gone_journals_probe_when_proc_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, journal, jid = _running_job()
    monkeypatch.setattr(coder_run, "_guest_pg_state", lambda _pgid: "gone")

    def _must_not_kill(_pgid: int, _sig: int) -> None:
        raise AssertionError("killpg must not run on probe when /proc says gone")

    monkeypatch.setattr(os, "killpg", _must_not_kill)
    assert coder_run._signal_or_record_gone(jobs, jid, pgid=4242, sig=0) == "gone"
    gone_events = [e for e in journal.events if e.type == "JobGuestProcessGroupGone"]
    assert len(gone_events) == 1
    assert gone_events[0].payload["op"] == "probe"
    assert gone_events[0].payload["sig_name"] == "0"
    jobs.close()


def test_signal_or_record_gone_killpg_toctou_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alive in /proc then ESRCH → typed refusal upward (not except→return)."""
    jobs, _journal, jid = _running_job()
    monkeypatch.setattr(coder_run, "_guest_pg_state", lambda _pgid: "alive")

    def _gone(_pgid: int, _sig: int) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr(os, "killpg", _gone)
    with pytest.raises(coder_run.GuestProcessGroupGone, match="pgid=4242.*SIGTERM"):
        coder_run._signal_or_record_gone(jobs, jid, pgid=4242, sig=signal.SIGTERM)
    jobs.close()


def test_signal_guest_pg_probe_oserror_is_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _denied(_pgid: int, _sig: int) -> None:
        raise PermissionError("simulated_probe_denied")

    monkeypatch.setattr(os, "killpg", _denied)
    with pytest.raises(ToolError, match="probe failed"):
        coder_run._signal_guest_pg(4242, 0)


def test_signal_guest_pg_killpg_oserror_is_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_pgid: int, _sig: int) -> None:
        raise OSError("simulated_killpg_failure")

    monkeypatch.setattr(os, "killpg", _boom)
    with pytest.raises(ToolError, match="killpg\\(SIGTERM\\) failed"):
        coder_run._signal_guest_pg(4242, signal.SIGTERM)


@pytest.mark.parametrize(
    ("pgid", "sig_name", "match"),
    [
        (0, "SIGTERM", "positive int"),
        (4242, " ", "non-empty bare"),
        (4242, "", "non-empty bare"),
    ],
    ids=["pgid_zero", "sig_whitespace", "sig_empty"],
)
def test_record_guest_process_group_gone_refuses(
    pgid: int,
    sig_name: str,
    match: str,
) -> None:
    jobs, _journal, jid = _running_job()
    with pytest.raises(ToolError, match=match):
        jobs.record_guest_process_group_gone(
            jid,
            pgid=pgid,
            op="killpg",
            sig_name=sig_name,
        )
    jobs.close()


def test_record_guest_process_group_gone_refuses_bad_op() -> None:
    jobs, _journal, jid = _running_job()
    kwargs: dict[str, object] = {"pgid": 4242, "op": "nope", "sig_name": "SIGTERM"}
    with pytest.raises(ToolError, match="probe\\|killpg"):
        jobs.record_guest_process_group_gone(jid, **kwargs)
    jobs.close()


@pytest.mark.asyncio
async def test_finally_probe_error_still_terminals_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ToolError from killpg in finally must not leave the job live."""
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")

    def _boom(_pgid: int, _sig: object) -> None:
        raise ToolError("local", "killpg(SIGTERM) failed: simulated")

    monkeypatch.setattr(coder_run, "_sandbox_worker", _worker_publish_pgid_no_result)
    monkeypatch.setattr(coder_run, "_guest_pg_state", lambda _pgid: "alive")
    monkeypatch.setattr(coder_run, "_signal_guest_pg", _boom)

    with pytest.raises(ToolError, match="exited without result"):
        await run_coder_job(
            CoderRequest(
                attempt_id="att#finally",
                call_id="call_f",
                argv=("/bin/true",),
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
    jid = _job_id(journal)
    assert not job_is_live(jobs.get(jid).status)
    assert jobs.get(jid).status == "failed"
    assert "JobCancelFailed" in [e.type for e in journal.events]
    reasons = [e.payload["reason"] for e in journal.events if e.type == "JobCancelFailed"]
    assert "guest_cleanup_failed" in reasons
    jobs.close()


@pytest.mark.asyncio
async def test_finally_kill_false_records_cancel_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finally must honor kill bool — surviving guest → JobCancelFailed."""
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    jobs = _jobs(clock, journal)
    arts = LocalArtifactStore(tmp_path / "cas")

    async def _alive_kill(
        _jobs: InMemoryJobStore,
        _jid: str,
        _proc: BaseProcess,
        *,
        guest_pgid: int,
        term_grace_s: float,
        reap_timeout_s: float,
        guest_done: asyncio.Event,
    ) -> bool:
        assert guest_pgid == 4242
        return False

    monkeypatch.setattr(coder_run, "_sandbox_worker", _worker_publish_pgid_no_result)
    monkeypatch.setattr(coder_run, "_guest_pg_state", lambda _pgid: "alive")
    monkeypatch.setattr(coder_run, "_kill_sandbox_guest", _alive_kill)

    with pytest.raises(ToolError, match="exited without result"):
        await run_coder_job(
            CoderRequest(
                attempt_id="att#alive",
                call_id="call_alive",
                argv=("/bin/true",),
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
    jid = _job_id(journal)
    assert not job_is_live(jobs.get(jid).status)
    assert jobs.get(jid).status == "failed"
    reasons = [e.payload["reason"] for e in journal.events if e.type == "JobCancelFailed"]
    assert "guest_still_alive" in reasons
    jobs.close()
