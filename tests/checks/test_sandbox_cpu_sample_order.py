"""Sandbox host-tree CPU must be sampled before poll() reaps /proc (PR17 follow-up)."""

from __future__ import annotations

import signal
from unittest.mock import MagicMock

import coscientist.checks.sandbox as sb
from coscientist.checks.sandbox import (
    SandboxLimits,
    SandboxStatus,
    _classify,
    _cpu_budget_seconds,
    _rlimit_cpu_seconds,
    _watch_process,
)


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
    import threading
    import time as time_mod

    released = threading.Event()

    def slow_read(
        pipe: object,
        limit: int,
        sink: list[bytes],
        overflow: list[bool],
    ) -> None:
        del pipe, limit, overflow
        assert released.wait(timeout=2.0)
        time_mod.sleep(0.05)
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

    lim = SandboxLimits(timeout_s=5.0, memory_mb=64, max_output_bytes=1024)
    _to, _ov, out, err, _kc, _cpu, _tree, drain_failed = _watch_process(
        FakeProc(),  # type: ignore[arg-type]
        lim=lim,
        cgroup_leaf=None,
    )
    assert out == b"999\n"
    assert err == b"999\n"
    assert drain_failed is False


def test_watch_process_stuck_reader_sets_output_drain_failed(monkeypatch):
    """Grandchild holding the pipe → reader stays alive; flag, do not silent b''."""
    import threading

    block = threading.Event()

    def stuck_read(
        pipe: object,
        limit: int,
        sink: list[bytes],
        overflow: list[bool],
    ) -> None:
        del pipe, limit, sink, overflow
        block.wait(timeout=30.0)

    class FakeProc:
        pid = 4242
        stdout = MagicMock()
        stderr = MagicMock()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(sb, "_OUTPUT_DRAIN_JOIN_S", 0.05)
    monkeypatch.setattr(sb, "_host_tree_cpu_s", lambda _pid: None)
    monkeypatch.setattr(sb, "_read_capped", stuck_read)
    monkeypatch.setattr(sb, "_cgroup_cpu_usage_usec", lambda leaf: None)

    lim = SandboxLimits(timeout_s=5.0, memory_mb=64, max_output_bytes=1024)
    _to, _ov, out, err, _kc, _cpu, _tree, drain_failed = _watch_process(
        FakeProc(),  # type: ignore[arg-type]
        lim=lim,
        cgroup_leaf=None,
    )
    block.set()
    assert drain_failed is True
    assert out == b""
    assert err == b""

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
    assert result.status in sb.INFRA_STATUSES


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
