"""Fail-closed cgroup leaf preparation (no silent loss of pids/cpu/subtree)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import coscientist.checks.sandbox as sb
import coscientist.checks.sandbox_cgroup as cg
from coscientist.checks import catalog as catalog_mod
from tests.checks.sandbox_test_limits import sandbox_limits


def _fake_cgroup_root(tmp_path: Path, *, controllers: str) -> Path:
    """Minimal cgroup v2 layout under ``tmp_path`` for prepare-leaf unit tests."""
    root = tmp_path / "cg"
    root.mkdir()
    (root / "cgroup.controllers").write_text(controllers)
    (root / "cgroup.subtree_control").write_text("")
    (root / "cgroup.procs").write_text("")
    return root


def _install_fake_cgroup(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(cg, "_cgroup_nest_root", lambda: root)
    monkeypatch.setattr(cg, "_cgroup_delegation_available", lambda: True)
    monkeypatch.setattr(cg, "_current_cgroup_path", lambda: root)


def test_prepare_failclosed_when_pids_controller_missing(tmp_path, monkeypatch):
    """pids absent → fatal missing; memory.max leaf kept for degraded OOM."""
    root = _fake_cgroup_root(tmp_path, controllers="memory cpu")
    _install_fake_cgroup(monkeypatch, root)
    # Leaf files appear after mkdir + subtree enable; simulate memory/cpu files.
    real_mkdir = Path.mkdir

    def mkdir_with_files(self, *a, **k):
        real_mkdir(self, *a, **k)
        (self / "memory.max").write_text("max")
        (self / "cpu.max").write_text("max 100000")
        (self / "cgroup.procs").write_text("")
        (self / "memory.events").write_text("oom_kill 0\n")

    monkeypatch.setattr(Path, "mkdir", mkdir_with_files)
    lim = sandbox_limits(
        timeout_s=5,
        memory_mb=64,
        max_output_bytes=1000,
        allow_cgroup_degraded=False,
    )
    prep = cg._cgroup_prepare_leaf(lim)
    assert "pids" in prep.missing
    assert prep.fatal is not None
    # Non-memory gaps keep the leaf with memory.max applied.
    assert prep.leaf is not None
    assert "memory" in prep.applied
    assert (prep.leaf / "memory.max").read_text().strip() != "max"
    cg._cgroup_remove_leaf(prep.leaf)


def test_prepare_cpuset_without_cpu_is_exact_token_failclosed(tmp_path, monkeypatch):
    """``cpuset`` must not satisfy substring ``'cpu' in controllers``."""
    # Controllers list has cpuset but not the cpu controller.
    root = _fake_cgroup_root(tmp_path, controllers="memory cpuset pids")
    _install_fake_cgroup(monkeypatch, root)
    real_mkdir = Path.mkdir

    def mkdir_with_files(self, *a, **k):
        real_mkdir(self, *a, **k)
        (self / "memory.max").write_text("max")
        (self / "pids.max").write_text("max")
        # No cpu.max — cpu controller absent.

    monkeypatch.setattr(Path, "mkdir", mkdir_with_files)
    lim = sandbox_limits(
        timeout_s=5,
        memory_mb=64,
        max_output_bytes=1000,
        allow_cgroup_degraded=False,
    )
    assert "cpu" not in cg._cgroup_controller_set("memory cpuset pids")
    assert "cpuset" in cg._cgroup_controller_set("memory cpuset pids")
    # Substring trap the old code hit:
    assert "cpu" in "memory cpuset pids"
    prep = cg._cgroup_prepare_leaf(lim)
    assert "cpu" in prep.missing
    assert prep.fatal is not None
    # memory still applied; leaf kept for degraded OOM.
    assert prep.leaf is not None
    assert "memory" in prep.applied
    assert "pids" in prep.applied
    cg._cgroup_remove_leaf(prep.leaf)


def test_prepare_failclosed_when_pids_max_write_fails(tmp_path, monkeypatch):
    root = _fake_cgroup_root(tmp_path, controllers="memory cpu pids")
    _install_fake_cgroup(monkeypatch, root)
    real_mkdir = Path.mkdir

    def mkdir_with_files(self, *a, **k):
        real_mkdir(self, *a, **k)
        (self / "memory.max").write_text("max")
        (self / "cpu.max").write_text("max 100000")
        (self / "cgroup.procs").write_text("")
        (self / "memory.events").write_text("oom_kill 0\n")
        (self / "pids.max").write_text("max")

    monkeypatch.setattr(Path, "mkdir", mkdir_with_files)

    real_write = Path.write_text

    def write_text(self, data, *a, **k):
        # Fail only the limit application write, not the seed "max" placeholder.
        if self.name == "pids.max" and str(data).strip() != "max":
            raise OSError("pids.max denied")
        return real_write(self, data, *a, **k)

    monkeypatch.setattr(Path, "write_text", write_text)
    lim = sandbox_limits(
        timeout_s=5,
        memory_mb=64,
        max_output_bytes=1000,
        allow_cgroup_degraded=False,
    )
    prep = cg._cgroup_prepare_leaf(lim)
    assert "pids.max" in prep.missing
    assert prep.fatal is not None
    # memory.max applied → leaf kept (pids-only gap).
    assert prep.leaf is not None
    assert "memory" in prep.applied
    cg._cgroup_remove_leaf(prep.leaf)


def test_prepare_failclosed_when_subtree_control_write_fails(tmp_path, monkeypatch):
    root = _fake_cgroup_root(tmp_path, controllers="memory cpu pids")
    _install_fake_cgroup(monkeypatch, root)
    real_write = Path.write_text

    def write_text(self, data, *a, **k):
        if self.name == "cgroup.subtree_control":
            raise OSError("subtree denied")
        return real_write(self, data, *a, **k)

    monkeypatch.setattr(Path, "write_text", write_text)
    lim = sandbox_limits(
        timeout_s=5,
        memory_mb=64,
        max_output_bytes=1000,
        allow_cgroup_degraded=False,
    )
    prep = cg._cgroup_prepare_leaf(lim)
    assert prep.leaf is None
    assert "subtree_control" in prep.missing
    assert prep.fatal == "subtree_control"


def test_prepare_failclosed_when_cpu_max_write_fails(tmp_path, monkeypatch):
    root = _fake_cgroup_root(tmp_path, controllers="memory cpu pids")
    _install_fake_cgroup(monkeypatch, root)
    real_mkdir = Path.mkdir

    def mkdir_with_files(self, *a, **k):
        real_mkdir(self, *a, **k)
        (self / "memory.max").write_text("max")
        (self / "cpu.max").write_text("max 100000")
        (self / "cgroup.procs").write_text("")
        (self / "memory.events").write_text("oom_kill 0\n")
        (self / "pids.max").write_text("max")

    monkeypatch.setattr(Path, "mkdir", mkdir_with_files)
    real_write = Path.write_text

    def write_text(self, data, *a, **k):
        if self.name == "cpu.max" and data != "max 100000":
            raise OSError("cpu.max denied")
        return real_write(self, data, *a, **k)

    monkeypatch.setattr(Path, "write_text", write_text)
    lim = sandbox_limits(
        timeout_s=5,
        memory_mb=64,
        max_output_bytes=1000,
        allow_cgroup_degraded=False,
    )
    prep = cg._cgroup_prepare_leaf(lim)
    assert "cpu.max" in prep.missing
    assert prep.fatal is not None
    assert prep.leaf is not None
    assert "memory" in prep.applied
    assert "pids" in prep.applied
    cg._cgroup_remove_leaf(prep.leaf)


def test_prepare_applies_pids_max_on_fake_cgroup(tmp_path, monkeypatch):
    """Separate proof of the cgroup ``pids.max`` write path (not RLIMIT_NPROC)."""
    root = _fake_cgroup_root(tmp_path, controllers="memory cpu pids")
    _install_fake_cgroup(monkeypatch, root)
    real_mkdir = Path.mkdir

    def mkdir_with_files(self, *a, **k):
        real_mkdir(self, *a, **k)
        (self / "memory.max").write_text("max")
        (self / "cpu.max").write_text("max 100000")
        (self / "cgroup.procs").write_text("")
        (self / "memory.events").write_text("oom_kill 0\n")
        (self / "pids.max").write_text("max")

    monkeypatch.setattr(Path, "mkdir", mkdir_with_files)
    lim = sandbox_limits(
        timeout_s=5,
        memory_mb=64,
        max_output_bytes=1000,
        pids_max=42,
        rlimit_nproc=9999,  # must not be what lands in pids.max
        allow_cgroup_degraded=False,
    )
    prep = cg._cgroup_prepare_leaf(lim)
    assert prep.fatal is None
    assert prep.leaf is not None
    assert "pids" in prep.applied
    assert (prep.leaf / "pids.max").read_text().strip() == "42"
    assert (prep.leaf / "pids.max").read_text().strip() != str(lim.rlimit_nproc)
    assert prep.missing == ()
    cg._cgroup_remove_leaf(prep.leaf)


def test_run_sandbox_failclosed_without_cgroup_delegation(monkeypatch):
    """allow_cgroup_degraded=False → SandboxInfraError when nest/CGROUP_JOB fails."""
    from coscientist.checks.sandbox import run_sandbox
    from coscientist.checks.sandbox_probe import probe_bwrap_hard
    from coscientist.core.events.errors import SandboxInfraError

    if not sb.bwrap_available() or not probe_bwrap_hard(allow_network=False):
        pytest.skip("bwrap hard required")

    def _no_nest() -> Path:
        raise SandboxInfraError("cgroup_job_required")

    monkeypatch.setattr(cg, "_cgroup_nest_root", _no_nest)
    lim = sandbox_limits(
        timeout_s=5,
        memory_mb=64,
        max_output_bytes=1000,
        allow_cgroup_degraded=False,
    )
    with pytest.raises(SandboxInfraError, match="cgroup_job_required"):
        run_sandbox(["/bin/true"], limits=lim,
        inputs={},
        env={},)


def test_run_sandbox_degraded_notes_missing_cgroup(monkeypatch):
    from coscientist.checks.sandbox import SandboxStatus, run_sandbox
    from coscientist.checks.sandbox_probe import probe_bwrap_hard
    from coscientist.core.events.errors import SandboxInfraError

    if not sb.bwrap_available() or not probe_bwrap_hard(allow_network=False):
        pytest.skip("bwrap hard required")

    def _no_nest() -> Path:
        raise SandboxInfraError("cgroup_job_required")

    monkeypatch.setattr(cg, "_cgroup_nest_root", _no_nest)
    lim = sandbox_limits(
        timeout_s=5,
        memory_mb=64,
        max_output_bytes=1000,
        allow_cgroup_degraded=True,
    )
    r = run_sandbox(["/bin/true"], limits=lim,
        inputs={},
        env={},)
    assert r.status is SandboxStatus.OK
    assert r.cgroup_limits_held is False
    assert r.limits_fully_held is False
    assert "cgroup" in r.missing_limits
    assert "cgroup limits missing" in (r.note or "")


def test_sigxcpu_derived_from_watch_poll_not_import_time():
    """slack/min come from SandboxLimits.watch_poll_interval_s (+ tick), not import."""
    tick = 1.0 / sb._clk_tck()
    poll = catalog_mod.SANDBOX_WATCH_POLL_INTERVAL_S
    slack_floor, min_m = sb._sigxcpu_from_poll(poll)
    assert min_m == pytest.approx(tick)
    assert slack_floor == pytest.approx(poll + tick)

    lim = sandbox_limits(timeout_s=5.0, memory_mb=64, max_output_bytes=1000)
    assert lim.sigxcpu_slack_s == pytest.approx(slack_floor)
    assert lim.sigxcpu_min_measured_s == pytest.approx(tick)
    assert (
        lim.cpu_budget_floor_s - lim.sigxcpu_slack_s >= lim.sigxcpu_min_measured_s
    )

    # poll=0.1 → slack floor 0.1+tick; raise cpu_budget_floor so gate stays valid.
    lim2 = sandbox_limits(
        timeout_s=5.0,
        memory_mb=64,
        max_output_bytes=1000,
        watch_poll_interval_s=0.1,
        cpu_budget_floor_s=0.2,
    )
    assert lim2.sigxcpu_slack_s == pytest.approx(0.1 + tick)
    assert lim2.sigxcpu_min_measured_s == pytest.approx(tick)
    assert lim2.sigxcpu_slack_s != lim.sigxcpu_slack_s

    with pytest.raises(ValueError, match="sigxcpu_slack_s"):
        sandbox_limits(
            timeout_s=5.0,
            memory_mb=64,
            max_output_bytes=1000,
            watch_poll_interval_s=0.1,
            cpu_budget_floor_s=0.2,
            sigxcpu_slack_s=0.05,  # < poll+tick
        )
    with pytest.raises(ValueError, match="sigxcpu_min_measured_s"):
        sandbox_limits(
            timeout_s=5.0,
            memory_mb=64,
            max_output_bytes=1000,
            cpu_budget_floor_s=0.05,
            sigxcpu_slack_s=1.0,
            sigxcpu_min_measured_s=0.5,
        )


def test_host_tree_cpu_sysconf_failure_raises(monkeypatch):
    """SC_CLK_TCK failure must raise like ``_clk_tck``, not return None."""

    def boom(_name: str) -> float:
        raise OSError("sysconf denied")

    monkeypatch.setattr(sb.os, "sysconf", boom)
    with pytest.raises(RuntimeError, match="SC_CLK_TCK unavailable"):
        sb._clk_tck()
    with pytest.raises(RuntimeError, match="SC_CLK_TCK unavailable"):
        sb._host_tree_cpu_s(os.getpid())


def test_cgroup_nest_root_respects_cgroup_job_ceiling(tmp_path, monkeypatch):
    """Nest uses ``CGROUP_JOB`` ceiling only; probe is fail-closed.

    Hostability is stubbed via ``_cgroup_can_host_leaf`` (raises or returns).
    """
    from coscientist.core.events.errors import SandboxInfraError

    job = tmp_path / "ci-job"
    worker = job / "worker"
    above = tmp_path / "sibling"
    job.mkdir()
    worker.mkdir()
    above.mkdir()
    monkeypatch.setattr(cg, "_current_cgroup_path", lambda: worker)

    def can_host(parent: Path) -> bool:
        if parent.resolve() != job.resolve():
            raise SandboxInfraError(f"cgroup_probe_host_leaf:OSError:{parent}")
        return True

    monkeypatch.setattr(cg, "_cgroup_can_host_leaf", can_host)

    monkeypatch.setenv("CGROUP_JOB", str(job))
    nest = cg._cgroup_nest_root()
    assert nest.resolve() == job.resolve()

    # Without CGROUP_JOB: required (fail-closed).
    monkeypatch.delenv("CGROUP_JOB", raising=False)
    with pytest.raises(SandboxInfraError, match="cgroup_job_required"):
        cg._cgroup_nest_root()

    # Mis-set CGROUP_JOB outside the process tree → typed refuse (not soft-None).
    monkeypatch.setenv("CGROUP_JOB", str(above))
    with pytest.raises(SandboxInfraError, match="cgroup_job_outside_process_tree"):
        cg._cgroup_nest_root()

    monkeypatch.setenv("CGROUP_JOB", str(job / "no-such-cg-dir"))
    with pytest.raises(SandboxInfraError, match="cgroup_job_not_directory"):
        cg._cgroup_nest_root()


def test_current_cgroup_path_oserror_and_missing_v2_entry_refuse(monkeypatch):
    """OSError on /proc/self/cgroup and missing 0:: → SandboxInfraError."""
    from coscientist.core.events.errors import SandboxInfraError

    real_is_file = Path.is_file
    real_read_text = Path.read_text

    def fake_is_file(self: Path) -> bool:
        if str(self) == "/sys/fs/cgroup/cgroup.controllers":
            return True
        return real_is_file(self)

    def boom_read(self: Path, *a: object, **k: object) -> str:
        if str(self) == "/proc/self/cgroup":
            raise OSError("boom")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "is_file", fake_is_file)
    monkeypatch.setattr(Path, "read_text", boom_read)
    with pytest.raises(SandboxInfraError, match="cgroup_read_proc"):
        cg._current_cgroup_path()

    def no_v2_entry(self: Path, *a: object, **k: object) -> str:
        if str(self) == "/proc/self/cgroup":
            return "1:name=systemd:/\n"
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", no_v2_entry)
    with pytest.raises(SandboxInfraError, match="cgroup_missing_v2_self_entry"):
        cg._current_cgroup_path()

def test_cgroup_job_empty_strip_raises(monkeypatch):
    from coscientist.core.events.errors import SandboxInfraError

    monkeypatch.setenv("CGROUP_JOB", "   ")
    with pytest.raises(SandboxInfraError, match="cgroup_job_whitespace_padded"):
        cg._cgroup_delegation_ceiling()


def test_cgroup_job_unset_raises(monkeypatch):
    from coscientist.core.events.errors import SandboxInfraError

    monkeypatch.delenv("CGROUP_JOB", raising=False)
    with pytest.raises(SandboxInfraError, match="cgroup_job_required"):
        cg._cgroup_delegation_ceiling()


def test_cgroup_can_host_leaf_oserror_is_infra(tmp_path, monkeypatch):
    """Probe I/O failure is SandboxInfraError — never soft-False."""
    from coscientist.core.events.errors import SandboxInfraError

    root = tmp_path / "cg"
    root.mkdir()
    real_mkdir = Path.mkdir

    def mkdir_boom(self: Path, *a: object, **k: object) -> None:
        if self.parent.resolve() == root.resolve() and self.name.startswith(
            "cos_sbx_probe_"
        ):
            raise OSError("probe denied")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", mkdir_boom)
    with pytest.raises(SandboxInfraError, match="cgroup_probe_host_leaf"):
        cg._cgroup_can_host_leaf(root)


def test_cgroup_nest_root_runtimeerror_surfaced_as_unavailable(monkeypatch):
    """CGROUP_JOB misconfig → SandboxInfraError from delegation probe."""
    from coscientist.core.events.errors import SandboxInfraError

    monkeypatch.setenv("CGROUP_JOB", "/no/such/cgroup/job")
    with pytest.raises(SandboxInfraError, match="cgroup_job"):
        cg._cgroup_delegation_available()
    prep = cg._cgroup_prepare_leaf(
        sandbox_limits(timeout_s=1.0, memory_mb=64, max_output_bytes=1024)
    )
    assert prep.leaf is None
    assert prep.fatal is not None
    assert "cgroup_job" in prep.fatal


def test_run_sandbox_degraded_keeps_memory_leaf_when_pids_missing(
    tmp_path, monkeypatch, caplog
):
    """Degraded mode: log.warning + keep memory.max leaf when only pids missing."""
    import logging

    from coscientist.checks.sandbox import SandboxStatus, run_sandbox
    from coscientist.checks.sandbox_probe import probe_bwrap_hard

    if not sb.bwrap_available() or not probe_bwrap_hard(allow_network=False):
        pytest.skip("bwrap hard required")

    root = _fake_cgroup_root(tmp_path, controllers="memory cpu")
    _install_fake_cgroup(monkeypatch, root)
    real_mkdir = Path.mkdir

    def mkdir_with_files(self, *a, **k):
        real_mkdir(self, *a, **k)
        (self / "memory.max").write_text("max")
        (self / "cpu.max").write_text("max 100000")
        (self / "cgroup.procs").write_text("")
        (self / "memory.events").write_text("oom_kill 0\n")
        (self / "cpu.stat").write_text("usage_usec 0\n")
        (self / "pids.peak").write_text("2\n")

    monkeypatch.setattr(Path, "mkdir", mkdir_with_files)
    real_remove = cg._cgroup_remove_leaf
    # Hold leaf across the run so we can assert memory.max stayed applied.
    monkeypatch.setattr(cg, "_cgroup_remove_leaf", lambda leaf: None)

    lim = sandbox_limits(
        timeout_s=5,
        memory_mb=64,
        max_output_bytes=1000,
        allow_cgroup_degraded=True,
    )
    with caplog.at_level(logging.WARNING, logger="coscientist.checks.sandbox"):
        r = run_sandbox(["/bin/true"], limits=lim,
        inputs={},
        env={},)
    assert r.status is SandboxStatus.OK
    assert r.cgroup_limits_held is False
    assert "pids" in r.missing_limits
    assert any("cgroup degraded mode" in m for m in caplog.messages)
    # Leaf still on disk with memory.max applied (not torn for pids-only gap).
    leaves = [p for p in root.iterdir() if p.is_dir() and p.name.startswith("cos_sbx_")]
    assert len(leaves) == 1
    mem = (leaves[0] / "memory.max").read_text().strip()
    assert mem == str(64 * 1024 * 1024)
    real_remove(leaves[0])


def test_cgroup_limits_missing_without_fatal_raises(monkeypatch):
    """Refuse soft ``else 'missing'`` when prep has missing tags but no fatal."""
    from coscientist.core.events.errors import SandboxInfraError

    monkeypatch.setattr(
        cg,
        "_cgroup_prepare_leaf",
        lambda _lim: cg._CgroupPrep(
            leaf=None,
            applied=frozenset(),
            missing=("pids",),
            fatal=None,
        ),
    )
    with pytest.raises(SandboxInfraError, match="missing_without_fatal"):
        sb._run_jailed(
            cmd=["/bin/true"],
            lim=sandbox_limits(
                timeout_s=1.0,
                memory_mb=64,
                max_output_bytes=1024,
                allow_cgroup_degraded=False,
            ),
            net_i="hard",
            fs_i="hard",
            work_dir=None,
            result_files_dir=None,
            collect=None,
        )
