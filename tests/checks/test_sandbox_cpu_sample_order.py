"""Sandbox host-tree CPU must be sampled before poll() reaps /proc (PR17 follow-up)."""

from __future__ import annotations

import math
import os
import select
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

import coscientist.checks.sandbox as sb
from coscientist.checks.kinds._util import raise_if_infra
from coscientist.checks.sandbox import (
    DEFAULT_OUTPUT_DRAIN_JOIN_S,
    DEFAULT_REAP_TIMEOUT_S,
    INFRA_STATUSES,
    SandboxLimits,
    SandboxStatus,
    _classify,
    _cpu_budget_seconds,
    _rlimit_cpu_seconds,
    _watch_process,
)
from coscientist.checks.sandbox_limits import apply_frozen_limits
from coscientist.core.execution.domain import CodeCheck

# Explicit test budgets — not reused from production grace/drain defaults.
TEST_WALL_TIMEOUT_S = 2.0
TEST_REAP_TIMEOUT_S = 0.25
TEST_DRAIN_JOIN_S = 0.25
TEST_LARGE_WALL_TIMEOUT_S = 30.0
TEST_READY_TIMEOUT_S = 5.0


def _read_ready(fd: int, *, timeout_s: float = TEST_READY_TIMEOUT_S) -> bytes:
    ready, _, _ = select.select([fd], [], [], timeout_s)
    assert ready, f"ready-pipe signal timed out after {timeout_s}s"
    return os.read(fd, 1)


def _force_reap_pid(pid: int, *, timeout_s: float) -> None:
    """SIGKILL then waitpid (children) or wait until /proc vanishes (orphans)."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + float(timeout_s)
    proc_path = f"/proc/{pid}"
    while time.monotonic() < deadline:
        try:
            wpid, _st = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                return
        except ChildProcessError:
            if not os.path.isdir(proc_path):
                return
        select.select([], [], [], min(0.05, max(0.0, deadline - time.monotonic())))


def test_watch_process_samples_host_cpu_before_poll(monkeypatch):
    """poll() reaps the child; /proc vanishes — sample must run first."""
    order: list[str] = []

    def fake_sample(pid: int) -> float:
        order.append("sample")
        return 0.42

    class FakeProc:
        pid = 4242
        stdout = MagicMock()
        stderr = MagicMock()
        _alive = True

        def poll(self):
            order.append("poll")
            if self._alive:
                self._alive = False
                return None
            return 0

        def wait(self, timeout=None):
            order.append("wait")
            return 0

    monkeypatch.setattr(sb, "_host_tree_cpu_s", fake_sample)
    monkeypatch.setattr(sb, "_read_capped", lambda *a, **k: None)
    monkeypatch.setattr(sb, "_cgroup_cpu_usage_usec", lambda leaf: None)

    lim = SandboxLimits(timeout_s=5.0, memory_mb=64, max_output_bytes=1024)
    timed_out, overflow, _out, _err, kill_cause, cpu_s, tree_cpu_s, drain_failed = (
        _watch_process(
            FakeProc(),  # type: ignore[arg-type]
            lim=lim,
            cgroup_leaf=None,
        )
    )

    assert timed_out is False
    assert overflow is False
    assert kill_cause is None
    assert cpu_s is None
    assert tree_cpu_s == 0.42
    assert drain_failed is False
    # Alive iteration samples then polls None; exit iteration samples then polls 0.
    assert order[:2] == ["sample", "poll"]
    assert order.count("sample") == 2
    assert order.count("poll") == 2
    assert order[-1] == "wait"


def test_watch_process_joins_readers_before_returning_stdout(monkeypatch):
    """Regression: empty stdout when sinks sampled before reader threads finish."""
    released = threading.Event()
    thread_errors: list[BaseException] = []
    lim = SandboxLimits(
        timeout_s=TEST_LARGE_WALL_TIMEOUT_S,
        memory_mb=64,
        max_output_bytes=1024,
        reap_timeout_s=TEST_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_DRAIN_JOIN_S,
    )

    def slow_read(
        pipe: object,
        limit: int,
        sink: list[bytes],
        overflow: list[bool],
    ) -> None:
        del pipe, limit, overflow
        if not released.wait(timeout=float(lim.timeout_s)):
            thread_errors.append(
                TimeoutError("release not signaled before drain wait budget")
            )
            return
        sink.append(b"999\n")

    class FakeProc:
        pid = 4242
        stdout = MagicMock()
        stderr = MagicMock()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            released.set()
            return 0

    monkeypatch.setattr(sb, "_host_tree_cpu_s", lambda _pid: None)
    monkeypatch.setattr(sb, "_read_capped", slow_read)
    monkeypatch.setattr(sb, "_cgroup_cpu_usage_usec", lambda leaf: None)

    _to, _ov, out, err, _kc, _cpu, _tree, drain_failed = _watch_process(
        FakeProc(),  # type: ignore[arg-type]
        lim=lim,
        cgroup_leaf=None,
    )
    assert not thread_errors, thread_errors
    assert out == b"999\n"
    assert err == b"999\n"
    assert drain_failed is False


def test_watch_process_stuck_reader_sets_output_drain_failed(monkeypatch):
    """Grandchild holding the pipe → reader stays alive; flag, do not silent b''."""
    release_readers = threading.Event()

    def stuck_read(
        pipe: object,
        limit: int,
        sink: list[bytes],
        overflow: list[bool],
    ) -> None:
        del pipe, limit, sink, overflow
        # Block until main releases after drain_failed assert (no time margin).
        release_readers.wait()

    class FakeProc:
        pid = 4242
        stdout = MagicMock()
        stderr = MagicMock()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(sb, "_host_tree_cpu_s", lambda _pid: None)
    monkeypatch.setattr(sb, "_read_capped", stuck_read)
    monkeypatch.setattr(sb, "_cgroup_cpu_usage_usec", lambda leaf: None)

    lim = SandboxLimits(
        timeout_s=TEST_LARGE_WALL_TIMEOUT_S,
        memory_mb=64,
        max_output_bytes=1024,
        reap_timeout_s=TEST_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_DRAIN_JOIN_S,
    )
    try:
        _to, _ov, out, err, _kc, _cpu, _tree, drain_failed = _watch_process(
            FakeProc(),  # type: ignore[arg-type]
            lim=lim,
            cgroup_leaf=None,
        )
        assert drain_failed is True
        assert out == b""
        assert err == b""
    finally:
        release_readers.set()

    proc = MagicMock()
    proc.returncode = 0
    result = _classify(
        timed_out=False,
        overflow=False,
        proc=proc,
        cgroup_leaf=None,
        oom_detectable=False,
        infra_msg=b"",
        out_b=out,
        err_b=err,
        duration=0.1,
        net_i="hard",
        fs_i="hard",
        work_dir=None,
        result_files={},
        output_drain_failed=True,
    )
    assert result.status is SandboxStatus.UNAVAILABLE
    assert result.reason == "sandbox_unavailable:output_drain"
    assert result.output_drain_failed is True
    assert result.status in INFRA_STATUSES


def test_classify_timeout_with_stuck_drain_stays_timeout_consumed():
    """Timeout + stuck drain must stay TIMEOUT (consumed), not UNAVAILABLE infra."""
    proc = MagicMock()
    proc.returncode = -1
    result = _classify(
        timed_out=True,
        overflow=False,
        proc=proc,
        cgroup_leaf=None,
        oom_detectable=False,
        infra_msg=b"",
        out_b=b"",
        err_b=b"",
        duration=1.0,
        net_i="hard",
        fs_i="hard",
        work_dir=None,
        result_files={},
        kill_cause="wall",
        cpu_usage_s=None,
        timeout_s=1.0,
        output_drain_failed=True,
    )
    assert result.status is SandboxStatus.TIMEOUT
    assert result.reason == "wall_timeout"
    assert result.output_drain_failed is True
    raise_if_infra(
        result.status,
        result.reason,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        note=result.note,
    )


def test_classify_overflow_with_stuck_drain_stays_output_exceeded_consumed():
    """Overflow + stuck drain must stay OUTPUT_EXCEEDED (consumed), not infra."""
    proc = MagicMock()
    proc.returncode = -9
    result = _classify(
        timed_out=False,
        overflow=True,
        proc=proc,
        cgroup_leaf=None,
        oom_detectable=False,
        infra_msg=b"",
        out_b=b"x" * 100,
        err_b=b"",
        duration=0.2,
        net_i="hard",
        fs_i="hard",
        work_dir=None,
        result_files={},
        output_drain_failed=True,
    )
    assert result.status is SandboxStatus.OUTPUT_EXCEEDED
    assert result.reason == "output_exceeded"
    assert result.output_drain_failed is True
    raise_if_infra(
        result.status,
        result.reason,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        note=result.note,
    )


def test_real_subprocess_grandchild_holds_stdout_sets_drain_failed():
    """No mocks: parent exits after fork; grandchild keeps stdout open."""
    ready_r, ready_w = os.pipe()
    script = (
        "import os, sys, time\n"
        f"ready_w = {ready_w}\n"
        "if os.fork() == 0:\n"
        "    os.write(ready_w, b'R')\n"
        "    os.close(ready_w)\n"
        "    time.sleep(3600)\n"
        "    os._exit(0)\n"
        "os.close(ready_w)\n"
        "sys.stdout.write('hi\\n')\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        pass_fds=(ready_w,),
    )
    os.close(ready_w)
    pgid = os.getpgid(proc.pid)
    lim = SandboxLimits(
        timeout_s=TEST_LARGE_WALL_TIMEOUT_S,
        memory_mb=64,
        max_output_bytes=1024,
        reap_timeout_s=TEST_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_DRAIN_JOIN_S,
    )
    try:
        assert _read_ready(ready_r) == b"R"
        os.close(ready_r)
        ready_r = -1
        # Synchronize on parent exit (not on wall timing) before drain join path.
        proc.wait(timeout=float(lim.timeout_s))
        timed_out, overflow, _out, _err, _kc, _cpu, _tree, drain_failed = (
            _watch_process(proc, lim=lim, cgroup_leaf=None)
        )
        assert timed_out is False
        assert overflow is False
        assert drain_failed is True
        assert _out == b""
        assert _err == b""
        result = _classify(
            timed_out=timed_out,
            overflow=overflow,
            proc=proc,
            cgroup_leaf=None,
            oom_detectable=False,
            infra_msg=b"",
            out_b=_out,
            err_b=_err,
            duration=float(lim.output_drain_join_s),
            net_i="hard",
            fs_i="hard",
            work_dir=None,
            result_files={},
            output_drain_failed=drain_failed,
        )
        assert result.status is SandboxStatus.UNAVAILABLE
        assert result.reason == "sandbox_unavailable:output_drain"
        assert result.output_drain_failed is True
    finally:
        if ready_r >= 0:
            try:
                os.close(ready_r)
            except OSError:
                pass
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=float(lim.reap_timeout_s))


def test_watch_timeout_with_stuck_drain_ready_pipe():
    """Ready pipe before watch; wall timeout + detached holder → wall_timeout."""
    ready_r, ready_w = os.pipe()
    holder_r, holder_w = os.pipe()
    script = (
        "import os, time\n"
        f"ready_w = {ready_w}\n"
        f"holder_w = {holder_w}\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    os.write(holder_w, str(os.getpid()).encode())\n"
        "    os.close(holder_w)\n"
        "    os.write(ready_w, b'R')\n"
        "    os.close(ready_w)\n"
        "    time.sleep(3600)\n"
        "    os._exit(0)\n"
        "os.close(ready_w)\n"
        "os.close(holder_w)\n"
        "time.sleep(3600)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        pass_fds=(ready_w, holder_w),
    )
    os.close(ready_w)
    os.close(holder_w)
    pgid = os.getpgid(proc.pid)
    lim = SandboxLimits(
        timeout_s=TEST_WALL_TIMEOUT_S,
        memory_mb=64,
        max_output_bytes=1024,
        reap_timeout_s=TEST_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_DRAIN_JOIN_S,
    )
    holder_pid: int | None = None
    try:
        # Holder must signal ready before watch starts (no fork race).
        assert _read_ready(ready_r) == b"R"
        os.close(ready_r)
        ready_r = -1
        ready_pid, _, _ = select.select([holder_r], [], [], TEST_READY_TIMEOUT_S)
        assert ready_pid, "holder pid pipe timed out"
        raw_pid = os.read(holder_r, 64)
        os.close(holder_r)
        holder_r = -1
        holder_pid = int(raw_pid.decode())
        timed_out, overflow, out, err, kill_cause, _cpu, _tree, drain_failed = (
            _watch_process(proc, lim=lim, cgroup_leaf=None)
        )
        assert timed_out is True
        assert kill_cause == "wall"
        assert overflow is False
        assert drain_failed is True
        result = _classify(
            timed_out=timed_out,
            overflow=overflow,
            proc=proc,
            cgroup_leaf=None,
            oom_detectable=False,
            infra_msg=b"",
            out_b=out,
            err_b=err,
            duration=float(lim.timeout_s),
            net_i="hard",
            fs_i="hard",
            work_dir=None,
            result_files={},
            kill_cause=kill_cause,
            cpu_usage_s=None,
            timeout_s=float(lim.timeout_s),
            output_drain_failed=drain_failed,
        )
        assert result.status is SandboxStatus.TIMEOUT
        assert result.reason == "wall_timeout"
        assert result.output_drain_failed is True
        raise_if_infra(
            result.status,
            result.reason,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            note=result.note,
        )
    finally:
        for fd in (ready_r, holder_r):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if holder_pid is not None:
            _force_reap_pid(holder_pid, timeout_s=float(lim.reap_timeout_s))
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=float(lim.reap_timeout_s))


@pytest.mark.parametrize(
    "field,value",
    [
        ("reap_timeout_s", True),
        ("reap_timeout_s", "5"),
        ("output_drain_join_s", True),
        ("output_drain_join_s", "5"),
    ],
)
def test_sandbox_limits_rejects_bool_str_host_budgets_typeerror(
    field: str, value: object
):
    with pytest.raises(TypeError, match=field):
        SandboxLimits(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value",
    [
        ("reap_timeout_s", 0.0),
        ("reap_timeout_s", -1.0),
        ("reap_timeout_s", math.inf),
        ("reap_timeout_s", math.nan),
        ("output_drain_join_s", 0.0),
        ("output_drain_join_s", -1.0),
        ("output_drain_join_s", math.inf),
        ("output_drain_join_s", math.nan),
    ],
)
def test_sandbox_limits_rejects_non_positive_host_budgets_valueerror(
    field: str, value: float
):
    with pytest.raises(ValueError, match=field):
        SandboxLimits(**{field: value})  # type: ignore[arg-type]


def test_apply_frozen_limits_rejects_coercible_host_budget_strings():
    from pydantic import ValidationError

    from coscientist.core.execution.domain import TaskSpec

    check = CodeCheck(kind="exec_runs", timeout_s=1.0, memory_mb=64)
    spec = TaskSpec(
        task_key="E1",
        title="t",
        goal="g",
        route="experiment",
        sandbox_check_limits={
            "exec_busy": {"reap_timeout_s": "5", "output_drain_join_s": True}
        },
    )
    with pytest.raises(ValidationError, match="host budget"):
        apply_frozen_limits(check, spec, template_id="exec_busy")


def test_limits_from_check_passes_host_budgets_or_dataclass_default():
    lim = sb.limits_from_check(
        timeout_s=1.0,
        memory_mb=64,
        max_output_bytes=1000,
        reap_timeout_s=TEST_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_DRAIN_JOIN_S,
    )
    assert lim.reap_timeout_s == TEST_REAP_TIMEOUT_S
    assert lim.output_drain_join_s == TEST_DRAIN_JOIN_S
    defaults = sb.limits_from_check(
        timeout_s=None, memory_mb=None, max_output_bytes=None
    )
    assert defaults.reap_timeout_s == DEFAULT_REAP_TIMEOUT_S
    assert defaults.output_drain_join_s == DEFAULT_OUTPUT_DRAIN_JOIN_S


def test_watch_process_keeps_max_sample_across_zero_and_none(monkeypatch):
    """Later 0.0 / None samples must not erase an earlier high watermark."""
    # Sequence consumed one-per-watch-iteration sample call.
    samples: list[float | None] = [0.9, 0.0, None]
    order: list[str] = []

    def fake_sample(pid: int) -> float | None:
        order.append("sample")
        return samples.pop(0) if samples else 0.0

    class FakeProc:
        pid = 4242
        stdout = MagicMock()
        stderr = MagicMock()
        _n_poll = 0

        def poll(self):
            order.append("poll")
            self._n_poll += 1
            # Stay alive for two None returns so we observe 0.9 → 0.0 → None.
            if self._n_poll <= 2:
                return None
            return 0

        def wait(self, timeout=None):
            order.append("wait")
            return 0

    monkeypatch.setattr(sb, "_host_tree_cpu_s", fake_sample)
    monkeypatch.setattr(sb, "_read_capped", lambda *a, **k: None)
    monkeypatch.setattr(sb, "_cgroup_cpu_usage_usec", lambda leaf: None)
    monkeypatch.setattr(sb.time, "sleep", lambda _s: None)

    lim = SandboxLimits(timeout_s=5.0, memory_mb=64, max_output_bytes=1024)
    _to, _ov, _o, _e, _kc, _cpu, tree_cpu_s, drain_failed = _watch_process(
        FakeProc(),  # type: ignore[arg-type]
        lim=lim,
        cgroup_leaf=None,
    )

    assert order[:2] == ["sample", "poll"]
    assert order.count("sample") == 3
    assert order.count("poll") == 3
    assert tree_cpu_s == 0.9
    assert drain_failed is False


def test_sigxcpu_without_cgroup_yields_cpu_timeout_when_rusage_at_budget():
    """No-cgroup SIGXCPU + host tree CPU ≥ RLIMIT budget → cpu_timeout (unit)."""
    timeout_s = 3.0
    rlim = _rlimit_cpu_seconds(timeout_s, cgroup_live=False)
    assert rlim is not None
    budget = _cpu_budget_seconds(timeout_s)
    assert float(rlim) < float(timeout_s)
    assert float(rlim) <= budget + 1e-9

    proc = MagicMock()
    proc.returncode = 128 + int(signal.SIGXCPU)  # shell-style 152

    result = _classify(
        timed_out=False,
        overflow=False,
        proc=proc,
        cgroup_leaf=None,
        oom_detectable=False,
        infra_msg=b"",
        out_b=b"",
        err_b=b"",
        duration=1.0,
        net_i="hard",
        fs_i="hard",
        work_dir=None,
        result_files={},
        kill_cause=None,
        cpu_usage_s=None,
        rusage_cpu_s=float(rlim),  # at integer RLIMIT edge
        timeout_s=timeout_s,
    )
    assert result.status is SandboxStatus.TIMEOUT
    assert result.reason == "cpu_timeout"
    assert result.timeout_kill == "cpu"
    assert result.cpu_usage_s == float(rlim)


def test_sigxcpu_without_cgroup_negligible_rusage_stays_nonzero():
    """Forged SIGXCPU with ~0 host tree CPU must not become cpu_timeout."""
    proc = MagicMock()
    proc.returncode = -int(signal.SIGXCPU)

    result = _classify(
        timed_out=False,
        overflow=False,
        proc=proc,
        cgroup_leaf=None,
        oom_detectable=False,
        infra_msg=b"",
        out_b=b"",
        err_b=b"",
        duration=0.1,
        net_i="hard",
        fs_i="hard",
        work_dir=None,
        result_files={},
        kill_cause=None,
        cpu_usage_s=None,
        rusage_cpu_s=0.0,
        timeout_s=5.0,
    )
    assert result.status is SandboxStatus.NONZERO
    assert result.reason == f"exit:{-int(signal.SIGXCPU)}"
