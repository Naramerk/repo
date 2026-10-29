"""Sandbox infra classify / drain fail-closed (#41)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import coscientist.checks.sandbox as sb
from coscientist.checks.sandbox import _classify
from tests.checks.sandbox_test_limits import (
    TEST_UNIT_MAX_OUTPUT_BYTES,
    TEST_UNIT_MEMORY_MB,
    sandbox_limits,
)


def _lim(timeout_s: float, **kwargs: object):
    return sandbox_limits(
        timeout_s=timeout_s,
        memory_mb=kwargs.pop("memory_mb", TEST_UNIT_MEMORY_MB),
        max_output_bytes=kwargs.pop("max_output_bytes", TEST_UNIT_MAX_OUTPUT_BYTES),
        **kwargs,
    )

def test_read_capped_notes_oserror_not_runtimeerror():
    """Drain OSError/ValueError keeps bytes + notes; other errors propagate."""

    class _Pipe:
        def __init__(self, err: BaseException):
            self._err = err

        def read(self, _n: int) -> bytes:
            raise self._err

    sink: list[bytes] = []
    overflow = [False]
    notes: list[str] = []
    sb._read_capped(_Pipe(OSError("closed")), 100, sink, overflow, notes)
    assert sink == [b""]
    assert notes == ["drain_OSError"]
    notes2: list[str] = []
    sb._read_capped(
        _Pipe(ValueError("I/O on closed file")), 100, [], overflow, notes2
    )
    assert notes2 == ["drain_ValueError"]
    with pytest.raises(RuntimeError, match="boom"):
        sb._read_capped(_Pipe(RuntimeError("boom")), 100, [], overflow, [])
    # drain_notes is required (TypeError if omitted).
    with pytest.raises(TypeError):
        sb._read_capped(_Pipe(OSError("x")), 100, [], overflow)  # type: ignore[call-arg]


def test_classify_empty_infra_kind_raises_sandbox_infra():
    """Empty infra payload kind → SandboxInfraError (no soft kind string)."""
    from coscientist.core.events.errors import SandboxInfraError

    proc = MagicMock()
    proc.returncode = 1
    with pytest.raises(SandboxInfraError, match="empty_infra_kind"):
        _classify(
            timed_out=False,
            overflow=False,
            proc=proc,
            cgroup_leaf=None,
            oom_detectable=False,
            infra_msg=sb._INFRA_MAGIC,
            out_b=b"",
            err_b=b"",
            duration=0.1,
            net_i="hard",
            fs_i="hard",
            work_dir=None,
            result_files={},
            kill_cause=None,
            cpu_usage_s=None,
            rusage_cpu_s=None,
            lim=_lim(5.0),
            cgroup_limits_held=True,
            )


def test_classify_missing_returncode_raises_sandbox_infra():
    """``returncode is None`` always raises — timeout included (no soft -1)."""
    from coscientist.core.events.errors import SandboxInfraError

    proc = MagicMock()
    proc.returncode = None
    with pytest.raises(SandboxInfraError, match="missing_returncode"):
        _classify(
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
            rusage_cpu_s=None,
            lim=_lim(5.0),
            cgroup_limits_held=True,
            )
    # Timeout with unreaped proc is still infra — never sentinel -1.
    with pytest.raises(SandboxInfraError, match="missing_returncode"):
        _classify(
            timed_out=True,
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
            kill_cause="wall",
            cpu_usage_s=None,
            rusage_cpu_s=None,
            lim=_lim(5.0),
            cgroup_limits_held=True,
            )



def test_cgroup_oom_events_read_oserror_is_infra(tmp_path):
    """B7: OOM events unreadable → SandboxInfraError (not soft False)."""
    from coscientist.core.events.errors import SandboxInfraError

    leaf = tmp_path / "leaf"
    leaf.mkdir()
    # memory.events absent → OSError path
    with pytest.raises(SandboxInfraError, match="cgroup_oom_events_read"):
        sb._cgroup_oom_killed(leaf)


def test_cgroup_oom_killed_none_leaf_is_infra():
    from coscientist.core.events.errors import SandboxInfraError

    with pytest.raises(SandboxInfraError, match="cgroup_oom_events_missing_leaf"):
        sb._cgroup_oom_killed(None)


def test_cgroup_oom_killed_missing_key_is_infra(tmp_path):
    from coscientist.core.events.errors import SandboxInfraError

    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "memory.events").write_text("oom_group_kill 0\n")
    with pytest.raises(SandboxInfraError, match="cgroup_oom_events_missing_oom_kill"):
        sb._cgroup_oom_killed(leaf)


def test_cgroup_oom_events_parse_bad_line_is_infra(tmp_path):
    from coscientist.core.events.errors import SandboxInfraError

    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "memory.events").write_text("oom_kill not-an-int\n")
    with pytest.raises(SandboxInfraError, match="cgroup_oom_events_parse"):
        sb._cgroup_oom_killed(leaf)


def test_read_nofollow_oserror_is_infra(tmp_path):
    from coscientist.core.events.errors import SandboxInfraError

    missing = tmp_path / "nope.txt"
    with pytest.raises(SandboxInfraError, match="read_nofollow"):
        sb._read_nofollow(missing)


def test_read_nofollow_non_regular_is_infra(tmp_path):
    from coscientist.core.events.errors import SandboxInfraError

    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(SandboxInfraError, match="not_regular_file"):
        sb._read_nofollow(d)


def test_wrapper_rlimit_unset_aborts_not_pass():
    import inspect

    src = inspect.getsource(sb._wrapper_script)
    assert 'else "pass"' not in src
    assert "rlimit_cpu_unset" in src
    # Force the unset branch: wall too small for a whole-second RLIMIT.
    lim = _lim(0.5)
    rlim = sb._rlimit_cpu_seconds(lim, cgroup_live=False)
    if rlim is None:
        script = sb._wrapper_script(lim=lim, cgroup_live=False)
        assert "raise OSError('rlimit_cpu_unset')" in script
        assert "resource.setrlimit(resource.RLIMIT_CPU" not in script


def test_run_sandbox_rejects_unsupported_kwargs():
    with pytest.raises(TypeError, match="core_files/collect_core/protect_paths"):
        sb.run_sandbox(
            ["/bin/true"],
            limits=_lim(1.0),
            inputs={},
            env={},
            core_files={"x": b"y"},
        )


def test_env_allowlist_rejects_coscientist_prefix_bypass(tmp_path):
    """Extra env must be in _ENV_ALLOWLIST — no COSCIENTIST_* prefix bypass."""
    from coscientist.checks import sandbox_probe as probe

    bwrap = probe._bwrap_bin()
    if bwrap is None:
        pytest.skip("bwrap missing")
    work = tmp_path / "work"
    work.mkdir()
    argv = probe.build_agent_bwrap(
        work=work,
        allow_network=False,
        extra_env=[
            ("COSCIENTIST_SECRET", "x"),
            ("PATH", "/usr/bin:/bin"),
        ],
    )
    joined = " ".join(argv)
    assert "COSCIENTIST_SECRET" not in joined
    assert argv.count("PATH") >= 1


def test_sandbox_result_requires_cgroup_limits_held():
    with pytest.raises(TypeError):
        sb.SandboxResult(  # type: ignore[call-arg]
            status=sb.SandboxStatus.OK,
            exit_code=0,
            stdout=b"",
            stderr=b"",
            reason="ok",
            duration_s=0.0,
        )


def test_verify_guest_memory_max_not_applied_is_infra(tmp_path):
    from coscientist.core.events.errors import SandboxInfraError

    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "memory.max").write_text("max")
    with pytest.raises(SandboxInfraError, match="oom_memory_max_not_applied"):
        sb._verify_guest_in_cgroup(
            leaf, 1, lim=_lim(1.0), bwrap_exited=lambda: False
        )


def test_cgroup_cpu_stat_missing_usage_is_infra(tmp_path):
    from coscientist.core.events.errors import SandboxInfraError

    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "cpu.stat").write_text("nr_periods 0\n")
    with pytest.raises(SandboxInfraError, match="cgroup_cpu_stat_missing_usage_usec"):
        sb._cgroup_cpu_usage_usec(leaf)


def test_open_nofollow_no_getattr_fallback():
    """B7: O_NOFOLLOW used directly — no getattr(..., 0) soft path."""
    import inspect

    src = inspect.getsource(sb._read_nofollow)
    assert "os.O_NOFOLLOW" in src
    assert "getattr(os" not in src and "getattr(os," not in src


def test_store_log_requires_cgroup_limits_held(tmp_path):
    """B10: cgroup_limits_held is required (no default True)."""
    from coscientist.checks.kinds._util import store_log
    from coscientist.core.execution.artifacts import LocalArtifactStore

    arts = LocalArtifactStore(tmp_path / "arts")
    with pytest.raises(TypeError):
        store_log(  # type: ignore[call-arg]
            arts,
            attempt_id="a1",
            check_id="c",
            body=b"x",
        )


def test_slot_filename_empty_refuses():
    from coscientist.checks.kinds._util import slot_filename
    from coscientist.core.events.errors import CatalogError

    with pytest.raises(CatalogError, match="empty_slot_filename"):
        slot_filename("")


def test_sandbox_effect_rejects_non_bool_countable():
    from coscientist.checks.kinds._util import sandbox_effect
    from coscientist.core.events.errors import CatalogError

    with pytest.raises(CatalogError, match="countable_must_be_bool"):
        sandbox_effect(
            attempt_id="a1",
            check_id="c",
            art_id="art",
            trusted=True,
            net_isolation="hard",
            fs_isolation="hard",
            note=None,
            countable=1,  # type: ignore[arg-type]
        )


def test_sandbox_probe_site_helpers_missing_raises(monkeypatch):
    """B11: missing site helpers → raise (no hasattr soft-empty)."""
    import site

    import coscientist.checks.sandbox_probe as probe

    monkeypatch.setattr(site, "getsitepackages", None, raising=False)
    # Force AttributeError on access pattern used by probe.
    class _Site:
        pass

    monkeypatch.setattr(probe, "site", _Site(), raising=False)
    # Re-import path: call helper after patching stdlib site used inside function.
    import importlib

    # Patch inside the function's import by replacing site module attrs.
    monkeypatch.delattr(site, "getsitepackages", raising=False)
    monkeypatch.delattr(site, "getusersitepackages", raising=False)

    def _boom(*_a, **_k):
        raise AttributeError("missing")

    # site is imported inside _python_site_ro_binds — patch the real module.
    importlib.reload(probe) if False else None
    monkeypatch.setattr(site, "getsitepackages", _boom, raising=False)
    monkeypatch.setattr(site, "getusersitepackages", _boom, raising=False)
    with pytest.raises(RuntimeError, match="site_helpers_missing"):
        probe._python_site_ro_binds()
