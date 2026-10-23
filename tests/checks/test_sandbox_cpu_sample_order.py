"""Sandbox host-tree CPU must be sampled before poll() reaps /proc (PR17 follow-up)."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import coscientist.checks.sandbox as sb
from coscientist.checks.sandbox import SandboxLimits, _watch_process


def test_watch_process_samples_host_cpu_before_poll(monkeypatch):
    """poll() reaps the child; /proc vanishes — sample must run first."""
    order: list[str] = []
    sample_calls = {"n": 0}

    def fake_sample(pid: int) -> float:
        order.append("sample")
        sample_calls["n"] += 1
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
    timed_out, overflow, _out, _err, kill_cause, cpu_s, tree_cpu_s = _watch_process(
        FakeProc(),  # type: ignore[arg-type]
        lim=lim,
        cgroup_leaf=None,
    )

    assert timed_out is False
    assert overflow is False
    assert kill_cause is None
    assert cpu_s is None
    assert tree_cpu_s == 0.42
    assert sample_calls["n"] >= 1
    # First observation must be sample before the first poll return-None cycle ends.
    assert order.index("sample") < order.index("poll")
