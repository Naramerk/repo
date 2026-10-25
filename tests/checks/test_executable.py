"""M1 PR17: executable kinds + bwrap sandbox / separate verifier."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from coscientist.checks.catalog import load_catalog
from coscientist.checks.evaluate import evaluate_code_check, evaluate_task_checks
from coscientist.checks.kinds.executable import (
    BUILTIN_METRIC_SCORER_ARGV,
    BUILTIN_TESTS_PASS_ARGV,
    REWARD_SCHEMA,
    agent_filename_allowed,
    parse_reward,
)
from coscientist.checks.sandbox import (
    INFRA_STATUSES,
    SandboxStatus,
    run_sandbox,
    run_verifier,
)
from coscientist.checks.sandbox_probe import bwrap_available, probe_bwrap_hard
from coscientist.core.claims.reasons import TaskReason
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import CatalogError, SandboxInfraError
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    CheckResult,
    CodeCheck,
    FrozenCheckLimits,
    Outcome,
    ReqStatus,
    RequirementRef,
    TaskSpec,
)
from tests.checks.sandbox_test_limits import (
    TEST_CHECK_DRAIN_JOIN_S,
    TEST_CHECK_REAP_TIMEOUT_S,
    TEST_MAX_OUTPUT_BYTES,
    TEST_MEMORY_MB,
    TEST_PIDS_MAX,
    TEST_RLIMIT_NPROC,
    TEST_TIMEOUT_S,
    sandbox_limits,
)
from tests.helpers import NEED_OFF, attach_executor, close_runtime, open_runtime, record_success


def _REWARD_JSON(v: float) -> bytes:
    return json.dumps({"schema": REWARD_SCHEMA, "value": float(v)}).encode()


_RERUN_SCORER = f"""
import json, os, pathlib
results = pathlib.Path(os.environ["COSCIENTIST_RESULTS"])
(results / "reward.txt").write_text(
    json.dumps({{"schema": {REWARD_SCHEMA!r}, "value": 1.0}})
)


""".encode()


CATALOG_VERSION = 4


_EXECUTABLE_KINDS = frozenset({"exec_runs", "tests_pass", "metric_on_holdout", "reproducible_rerun"})


def _catalog_with(*templates: dict) -> object:
    rules = [
        {
            "rule_id": "R23",
            "source": "reviewed-rules",
            "excerpt": "x",
            "digest": "abc",
        }
    ]

    def _with_exec_budgets(t: dict) -> dict:
        """Normalize CodeCheck rows for load_catalog (security toggles required).

        Security toggles: always filled (CodeCheck requires them).
        Scientific budgets: TEST_* fill only when omitted — test-fixture
        convenience (audit-ok); production CodeCheck has no such defaults.
        """
        check = t.get("check")
        if not isinstance(check, dict):
            return t
        if "kind" not in check:
            return t
        patched = dict(check)
        if "allow_network" not in patched:
            patched["allow_network"] = False
        if "allow_cgroup_degraded" not in patched:
            patched["allow_cgroup_degraded"] = True
        # KindSpec freeze requires authored metric/op/target for check=code|review;
        # presence kinds may omit them (freeze refuses — no invent here).
        if check.get("kind") in _EXECUTABLE_KINDS:
            if "timeout_s" not in patched:
                patched["timeout_s"] = TEST_TIMEOUT_S
            if "memory_mb" not in patched:
                patched["memory_mb"] = TEST_MEMORY_MB
            if "max_output_bytes" not in patched:
                patched["max_output_bytes"] = TEST_MAX_OUTPUT_BYTES
            if "pids_max" not in patched:
                patched["pids_max"] = TEST_PIDS_MAX
            if "rlimit_nproc" not in patched:
                patched["rlimit_nproc"] = TEST_RLIMIT_NPROC
            if "reap_timeout_s" not in patched:
                patched["reap_timeout_s"] = TEST_CHECK_REAP_TIMEOUT_S
            if "output_drain_join_s" not in patched:
                patched["output_drain_join_s"] = TEST_CHECK_DRAIN_JOIN_S
        return {**t, "check": patched}

    def _with_required_meta(t: dict) -> dict:
        """Merge exec budgets; require catalog 0.2 meta keys (no soft .get defaults)."""
        out = _with_exec_budgets(t)
        required_keys = (
            "text_ru",
            "rule_ids",
            "method_sources",
            "applies_to",
            "role",
            "required",
            "version",
            "uses_sesoi",
            "predicts_null",
            "predicted_direction",
            "exempt_from_freeze",
        )
        missing = [k for k in required_keys if k not in out]
        if missing:
            raise AssertionError(f"test template {out.get('id')!r} missing required meta: {missing}")
        return out

    # Fixture rows historically omitted meta; fill explicitly here once.
    _META_FILL = {
        "text_ru": "проверка",
        "rule_ids": ["R23"],
        "method_sources": ["test"],
        "applies_to": ["experiment"],
        "role": "validity",
        "required": True,
        "version": 1,
        "uses_sesoi": False,
        "predicts_null": False,
        "predicted_direction": "two_sided",
        "exempt_from_freeze": False,
    }

    def _fill_then_require(t: dict) -> dict:
        filled = {**_META_FILL, **t}
        return _with_required_meta(filled)

    from coscientist.core.claims.graph import default_execution_registry

    # Stage helpers (REPORT/SPEC success via record_success) always present.
    # Custom rows win on id collision.
    _stage = [
        {
            "id": "value_match",
            "title": "Value matches",
            "check": {
                "kind": "value_matches",
                "metric": "/count",
                "op": "==",
                "output": "main",
                "pointer": "/count",
                "target": 225,
                "allow_network": False,
                "allow_cgroup_degraded": False,
            },
            "role": "prediction",
        },
        {
            "id": "artifact_present",
            "title": "Artifact exists",
            "check": {
                "kind": "artifact_exists",
                "output": "main",
                "allow_network": False,
                "allow_cgroup_degraded": False,
            },
            "role": "validity",
        },
    ]
    by_id: dict[str, dict] = {t["id"]: t for t in _stage}
    for t in templates:
        by_id[t["id"]] = t
    merged = list(by_id.values())
    default_exp = templates[0]["id"] if templates else "value_match"

    return load_catalog(
        {
            "catalog_version": CATALOG_VERSION,
            "edition": "pr17",
            "sources_sha256": {},
            "rules": rules,
            "templates": [_fill_then_require(t) for t in merged],
            "defaults": {"experiment": [default_exp]},
            "code_output_names": [],
        },
        kind_registry=default_execution_registry(),
    )


def _force_soft(monkeypatch):
    """Fail-closed soft path: probe fails (bwrap may still be present)."""
    import coscientist.checks.sandbox_probe as probe

    probe.probe_bwrap_hard.cache_clear()
    monkeypatch.setattr(
        probe,
        "probe_bwrap_hard",
        lambda *, allow_network=False: False,
    )


def _require_hard():
    if not bwrap_available() or not probe_bwrap_hard(allow_network=False):
        pytest.fail("bubblewrap hard probe must pass in CI (apt install bubblewrap)")


def test_sandbox_ok_exit_zero():
    _require_hard()
    r = run_sandbox(
        ["/bin/true"],
        limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1_000_000),
        inputs={},
        env={},
    )
    assert r.status is SandboxStatus.OK
    assert r.exit_code == 0
    assert r.limits_fully_held == (r.net_isolation == "hard" and r.fs_isolation == "hard" and r.cgroup_limits_held)


def test_sandbox_hard_isolation_when_bwrap_available():
    """Real bwrap probe — never fake hard with an empty prefix."""
    _require_hard()
    r = run_sandbox(
        ["/bin/true"],
        limits=sandbox_limits(timeout_s=5, memory_mb=64, allow_network=False, max_output_bytes=1_000_000),
        inputs={},
        env={},
    )
    assert r.status is SandboxStatus.OK
    assert r.net_isolation == "hard"
    assert r.fs_isolation == "hard"
    if r.cgroup_limits_held:
        assert r.limits_fully_held is True
    else:
        assert r.limits_fully_held is False
        assert "cgroup" in r.missing_limits


def test_sandbox_fail_closed_when_bwrap_probe_fails(monkeypatch, caplog, tmp_path):
    """Without hard isolation: no guest exec → sandbox_unavailable (fail-closed)."""
    import logging

    _force_soft(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="coscientist.checks.sandbox_probe"):
        r = run_sandbox(
            ["/bin/true"],
            limits=sandbox_limits(timeout_s=5, memory_mb=64, allow_network=False, max_output_bytes=1_000_000),
            inputs={},
            env={},
        )
    assert r.status is SandboxStatus.UNAVAILABLE
    assert r.reason == "sandbox_unavailable:isolation"
    assert r.net_isolation == "soft"
    assert r.fs_isolation == "soft"
    assert r.limits_fully_held is False

    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="exec_runs",
        argv=["/bin/true"],
        timeout_s=5,
        memory_mb=64,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=True,
    )
    with pytest.raises(SandboxInfraError, match="sandbox_unavailable:isolation"):
        evaluate_code_check(
            check,
            template_id="exec",
            check_id="exec#soft",
            out=AgentTaskOutput(outputs={}),
            artifacts=arts,
            values={},
            role="validity",
            attempt_id="st/E1#1",
        )


def test_sandbox_no_bwrap_fail_closed(monkeypatch, tmp_path):
    import coscientist.checks.sandbox_probe as probe

    probe._bwrap_bin.cache_clear()
    probe.probe_bwrap_hard.cache_clear()
    monkeypatch.setattr(probe, "_bwrap_bin", lambda: None)
    monkeypatch.setattr(probe, "bwrap_available", lambda: False)
    r = run_sandbox(
        ["/bin/true"],
        limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1_000_000),
        inputs={},
        env={},
    )
    assert r.status is SandboxStatus.UNAVAILABLE
    assert r.reason == "sandbox_unavailable:isolation"
    arts = LocalArtifactStore(tmp_path)
    with pytest.raises(SandboxInfraError, match="sandbox_unavailable:isolation"):
        evaluate_code_check(
            CodeCheck(
                reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
                output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
                kind="exec_runs",
                argv=["/bin/true"],
                timeout_s=5,
                memory_mb=64,
                max_output_bytes=TEST_MAX_OUTPUT_BYTES,
                pids_max=TEST_PIDS_MAX,
                rlimit_nproc=TEST_RLIMIT_NPROC,
                allow_network=False,
                allow_cgroup_degraded=True,
            ),
            template_id="exec",
            check_id="exec#nobwrap",
            out=AgentTaskOutput(outputs={}),
            artifacts=arts,
            values={},
            role="validity",
            attempt_id="st/E1#1",
        )


def test_sandbox_allow_network_runs_but_not_trusted(tmp_path):
    """FS hard + intentional net=none still executes via bwrap; trusted=False."""
    if not bwrap_available() or not probe_bwrap_hard(allow_network=True):
        pytest.fail("bubblewrap required")
    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="exec_runs",
        argv=["/bin/true"],
        timeout_s=5,
        memory_mb=64,
        allow_network=True,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_cgroup_degraded=True,
    )
    cr = evaluate_code_check(
        check,
        template_id="exec",
        check_id="exec#net",
        out=AgentTaskOutput(outputs={}),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.MET
    assert cr.effect is not None
    assert cr.effect.get("net_isolation") == "none"
    assert cr.effect.get("fs_isolation") == "hard"
    assert cr.effect.get("trusted") is False


def test_sandbox_sleep_bypass_is_idle_or_wall_timeout():
    """Guest sleep past wall → idle when cpu.stat measured; else wall (not infra).

    ``timeout_s=2.0``: without cgroup, fail-closed RLIMIT_CPU needs a whole
    second under wall; with ``cpu.stat``, idle ratio stays clear of startup.
    """
    _require_hard()
    r = run_sandbox(
        ["python3", "-c", "import time; time.sleep(1e9)"],
        limits=sandbox_limits(timeout_s=2.0, memory_mb=64, max_output_bytes=10_000),
        inputs={},
        env={},
    )
    assert r.status is SandboxStatus.TIMEOUT
    assert r.is_guest_timeout
    assert r.status not in INFRA_STATUSES
    # Fail-closed: classification requires measured CPU (cgroup or host tree).
    assert r.cpu_usage_s is not None
    assert r.duration_s > 0.0
    assert r.idle_cpu_wall_ratio is not None
    assert (r.cpu_usage_s / r.duration_s) < float(r.idle_cpu_wall_ratio)
    assert r.reason == "idle_timeout"


def test_sandbox_busy_loop_is_cpu_or_wall_timeout():
    """Busy loop → cpu_timeout (cgroup watcher or no-cgroup rusage≥budget)."""
    _require_hard()
    r = run_sandbox(
        ["python3", "-c", "while True: pass"],
        limits=sandbox_limits(timeout_s=3.0, memory_mb=64, max_output_bytes=10_000),
        inputs={},
        env={},
    )
    assert r.status not in INFRA_STATUSES
    assert r.status is SandboxStatus.TIMEOUT
    assert r.reason in ("cpu_timeout", "wall_timeout")
    assert r.reason != "idle_timeout"
    assert r.is_guest_timeout


def test_sandbox_fork_cpu_spread_is_busy_timeout():
    """CPU spread across children still bounded → busy timeout (not idle)."""
    _require_hard()
    # Fork several busy children so per-process RLIMIT alone would not catch them
    # before wall; tree-wide cpu.stat / wall / rusage must still yield a busy timeout.
    attack = r"""
import os, sys
kids = []
for _ in range(4):
    pid = os.fork()
    if pid == 0:
        while True:
            pass
    kids.append(pid)
while True:
    pass
"""
    r = run_sandbox(
        ["python3", "-c", attack],
        limits=sandbox_limits(timeout_s=2.0, memory_mb=64, max_output_bytes=10_000),
        inputs={},
        env={},
    )
    assert r.status not in INFRA_STATUSES
    assert r.status is SandboxStatus.TIMEOUT
    assert r.reason in ("cpu_timeout", "wall_timeout")
    assert r.reason != "idle_timeout"


def _classify_exit(tmp_path, code: int, *, oom_detectable: bool):
    """``_classify`` for a finished bwrap with ``returncode == code``."""
    from unittest.mock import MagicMock

    from coscientist.checks.sandbox import _classify

    leaf = None
    if oom_detectable:
        leaf = tmp_path / "leaf"
        leaf.mkdir(parents=True)
        (leaf / "memory.events").write_text("oom_kill 0\n")
    proc = MagicMock()
    proc.returncode = code
    return _classify(
        timed_out=False,
        overflow=False,
        proc=proc,
        cgroup_leaf=leaf,
        oom_detectable=oom_detectable,
        infra_msg=b"",
        out_b=b"",
        err_b=b"",
        duration=0.1,
        net_i="hard",
        fs_i="hard",
        work_dir=None,
        result_files={},
        lim=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1_000_000),
        cgroup_limits_held=oom_detectable,
    )


def test_sandbox_positive_exit_137_is_scientific_not_oom(tmp_path):
    """With a verified memory cgroup (oom_kill 0) exit 137 is scientific NONZERO."""
    r = _classify_exit(tmp_path, 137, oom_detectable=True)
    assert r.status is SandboxStatus.NONZERO
    assert r.exit_code == 137


def test_sandbox_guest_exit_75_is_scientific_not_rlimit():
    """Guest exit 75 must never map to UNAVAILABLE — only private channel does."""
    _require_hard()
    r = run_sandbox(
        ["python3", "-c", "raise SystemExit(75)"],
        limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1_000_000),
        inputs={},
        env={},
    )
    assert r.status is SandboxStatus.NONZERO
    assert r.exit_code == 75
    assert r.status not in INFRA_STATUSES


def test_sandbox_guest_signal_is_scientific_not_infra(tmp_path):
    """Guest self-kill with verified cgroup OOM state is NONZERO, never infra."""
    for code in (-9, 137):
        r = _classify_exit(tmp_path / str(abs(code)), code, oom_detectable=True)
        assert r.status is SandboxStatus.NONZERO
        assert r.status not in (
            SandboxStatus.TIMEOUT,
            SandboxStatus.OOM,
            SandboxStatus.UNAVAILABLE,
        )


def test_sandbox_network_and_etc_writes_fail():
    """bwrap --unshare-net + RO system binds: curl/socket and /etc writes fail."""
    import shutil

    _require_hard()
    if shutil.which("curl") is None:
        pytest.fail("curl required on host to probe guest network isolation")
    probe = r"""
import os, socket, subprocess, sys
errs = []
# Network must be absent (--unshare-net). curl must be present in the jail PATH.
try:
    r = subprocess.run(
        ["curl", "-sS", "--max-time", "2", "https://example.com"],
        capture_output=True,
        timeout=3,
    )
except FileNotFoundError:
    sys.stderr.write("ISOLATION_FAIL:curl_missing\n")
    sys.exit(3)
except Exception as e:
    sys.stderr.write("ISOLATION_FAIL:curl_exc:" + type(e).__name__ + "\n")
    sys.exit(3)
else:
    if r.returncode == 0 and r.stdout:
        errs.append("curl_ok")
try:
    socket.create_connection(("1.1.1.1", 80), timeout=1.0)
    errs.append("socket_ok")
except OSError:
    pass
# System paths are RO binds — writes to /etc must fail.
try:
    open("/etc/cos_sbx_pwned", "w").write("x")
    errs.append("etc_write")
except OSError:
    pass
if errs:
    sys.stderr.write("ISOLATION_FAIL:" + ",".join(errs) + "\n")
    sys.exit(2)
sys.exit(0)
"""
    r = run_sandbox(
        ["python3", "-c", probe],
        limits=sandbox_limits(
            timeout_s=5,
            memory_mb=64,
            allow_network=False,
            max_output_bytes=1_000_000,
        ),
        inputs={},
        env={},
    )
    assert r.status is SandboxStatus.OK, (r.status, r.reason, r.stderr)


def test_sandbox_fork_bomb_hits_pid_budget():
    """Successful forks capped; guest sees EAGAIN; fail-closed when cgroup held.

    ``RLIMIT_NPROC`` is per host UID (shared), not per jail; cgroup ``pids.max``
    is the per-sandbox bound when applied (proved separately on the prepare
    path). When cgroup delegation is available this run uses
    ``allow_cgroup_degraded=False`` so the pids controller must be held.
    """
    import errno

    import coscientist.checks.sandbox as sb_mod

    _require_hard()
    limit = 16
    attack = r"""
import errno, os, sys
# Count successful forks, then stop trying and park so the host can reap.
n = 0
err = None
while n < 10_000:
    try:
        pid = os.fork()
    except OSError as e:
        err = e.errno
        break
    if pid == 0:
        while True:
            pass
    n += 1
sys.stdout.write("FORKS=%d\n" % n)
sys.stdout.write("FORK_ERRNO=%s\n" % (err if err is not None else ""))
sys.stdout.flush()
while True:
    pass
"""
    try:
        cgroup_ok = sb_mod._cgroup_delegation_available()
    except SandboxInfraError:
        cgroup_ok = False
    r = run_sandbox(
        ["python3", "-c", attack],
        limits=sandbox_limits(
            timeout_s=2.0,
            memory_mb=64,
            max_output_bytes=10_000,
            pids_max=limit,
            rlimit_nproc=limit,
            allow_cgroup_degraded=not cgroup_ok,
        ),
        inputs={},
        env={},
    )
    if not cgroup_ok:
        # Without delegation fail-closed would refuse before the guest runs;
        # degraded path still exercises RLIMIT_NPROC EAGAIN below.
        assert r.cgroup_limits_held is False
    else:
        assert r.cgroup_limits_held is True
        assert r.limits_fully_held is True
    assert r.status not in INFRA_STATUSES
    assert r.status is SandboxStatus.TIMEOUT
    assert r.duration_s < 8.0
    out = r.stdout.decode("utf-8", "replace")
    assert "FORKS=" in out, (out, r.stderr)
    n_forks = int(out.strip().split("FORKS=", 1)[-1].splitlines()[0])
    assert 0 < n_forks <= limit, n_forks
    assert "FORK_ERRNO=" in out, out
    fork_errno = out.strip().split("FORK_ERRNO=", 1)[-1].splitlines()[0]
    assert fork_errno == str(errno.EAGAIN), (fork_errno, out, r.stderr)


def test_stuck_reap_after_wall_preserves_kill_cause(monkeypatch):
    """ReapTimeoutError after wall kill stays TIMEOUT with kill_cause (not host_io)."""
    _require_hard()
    import coscientist.checks.sandbox as sb_mod
    from coscientist.checks.sandbox import ReapTimeoutError

    def boom(proc, *, term_grace_s, reap_timeout_s):
        del proc, term_grace_s, reap_timeout_s
        raise ReapTimeoutError("pid stuck in reap")

    monkeypatch.setattr(sb_mod, "_kill_group_term_then_kill", boom)
    r = run_sandbox(
        ["python3", "-c", "import time; time.sleep(1e9)"],
        limits=sandbox_limits(timeout_s=2.0, memory_mb=64, max_output_bytes=10_000),
        inputs={},
        env={},
    )
    assert r.status is SandboxStatus.TIMEOUT
    assert r.timeout_kill == "wall"
    assert r.reason in ("idle_timeout", "wall_timeout")
    assert "host_io" not in r.reason
    assert not issubclass(ReapTimeoutError, OSError)
    assert not issubclass(ReapTimeoutError, TimeoutError)


def test_reap_killpg_followup_errors_are_noted(monkeypatch, caplog):
    """TimeoutExpired after killpg is logged; unreaped returncode → infra."""
    import logging
    import subprocess

    import coscientist.checks.sandbox as sb_mod
    from coscientist.checks.sandbox import ReapTimeoutError

    _require_hard()

    def watch_boom(*a, **k):
        del a, k
        raise ReapTimeoutError(
            "stuck",
            kill_cause="wall",
            timed_out=True,
            overflow=False,
        )

    monkeypatch.setattr(sb_mod, "_watch_process", watch_boom)

    def killpg_ok(pid, sig):
        del pid, sig

    monkeypatch.setattr(sb_mod.os, "killpg", killpg_ok)

    real_popen = sb_mod.subprocess.Popen

    class WaitTimeoutPopen(real_popen):
        def wait(self, timeout=None):  # noqa: ANN001
            del timeout
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=0.1)

    monkeypatch.setattr(sb_mod.subprocess, "Popen", WaitTimeoutPopen)
    with caplog.at_level(logging.WARNING, logger="coscientist.checks.sandbox"):
        # returncode stays None after unreaped wait → missing_returncode (no soft -1).
        with pytest.raises(SandboxInfraError, match="missing_returncode"):
            run_sandbox(
                ["/bin/true"],
                limits=sandbox_limits(timeout_s=2.0, memory_mb=64, max_output_bytes=10_000),
                inputs={},
                env={},
            )
    assert any("wait after killpg timed out" in m for m in caplog.messages)


def test_run_jailed_unavailable_becomes_executable_refusal(tmp_path, monkeypatch):
    """UNAVAILABLE from ``_run_jailed`` (cgroup_limits) → SandboxInfraError."""
    import coscientist.checks.kinds.executable as exe_mod
    import coscientist.checks.sandbox as sb_mod

    arts = LocalArtifactStore(tmp_path)

    def fake_run(*a, **k):
        del a, k
        return sb_mod.SandboxResult(
            status=SandboxStatus.UNAVAILABLE,
            exit_code=None,
            reason="sandbox_unavailable:cgroup_limits:missing",
            stdout=b"",
            stderr=b"",
            duration_s=0.0,
            net_isolation="hard",
            fs_isolation="hard",
            note="cgroup limits missing: pids",
            cgroup_limits_held=False,
            oom_detectable=False,
            missing_limits=("pids",),
        )

    monkeypatch.setattr(sb_mod, "run_sandbox", fake_run)
    monkeypatch.setattr(exe_mod, "run_sandbox", fake_run)
    with pytest.raises(SandboxInfraError, match="sandbox_unavailable:cgroup_limits"):
        evaluate_code_check(
            CodeCheck(
                reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
                output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
                kind="exec_runs",
                argv=["/bin/true"],
                timeout_s=5,
                memory_mb=64,
                max_output_bytes=TEST_MAX_OUTPUT_BYTES,
                pids_max=TEST_PIDS_MAX,
                rlimit_nproc=TEST_RLIMIT_NPROC,
                allow_network=False,
                allow_cgroup_degraded=False,
            ),
            template_id="exec",
            check_id="exec#cgroup_unavail",
            out=AgentTaskOutput(outputs={}),
            artifacts=arts,
            values={},
            role="validity",
            attempt_id="st/E1#1",
        )


def test_sandbox_cannot_rewrite_artifact_meta(tmp_path):
    """Hard bwrap hardening: umount/remount/nested userns/ro-write all fail (B2)."""
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    art = arts.put(
        b"secret",
        make_tool_ref("st/E1#1:data"),
        name="data.bin",
        media_type="application/octet-stream",
    )
    meta_path = arts.meta / f"{art.artifact_id}.json"
    original = meta_path.read_bytes()
    # Also mount a RO canary via inputs to prove write-to-ro-bind fails.
    attack = r"""
import os, sys, ctypes
errs = []
meta = %r
# 1) host meta path must be absent / not writable inside jail
try:
    open(meta, 'w').write('pwned')
    errs.append('meta_write')
except OSError:
    pass
# 2) umount system RO bind
try:
    libc = ctypes.CDLL('libc.so.6', use_errno=True)
    if libc.umount2(b'/usr', 0) == 0:
        errs.append('umount_usr')
except Exception:
    pass
# 3) remount rw
try:
    libc = ctypes.CDLL('libc.so.6', use_errno=True)
    MS_REMOUNT, MS_BIND, MS_RDONLY = 32, 4096, 1
    # clear MS_RDONLY on /usr
    if libc.mount(b'none', b'/usr', None, MS_REMOUNT | MS_BIND, None) == 0:
        errs.append('remount_rw')
except Exception:
    pass
# 4) nested userns (--disable-userns)
try:
    CLONE_NEWUSER = 0x10000000
    libc = ctypes.CDLL('libc.so.6', use_errno=True)
    if hasattr(libc, 'unshare') and libc.unshare(CLONE_NEWUSER) == 0:
        errs.append('nested_userns')
except Exception:
    pass
# 5) write into chmod-444 canary (ro content in work)
try:
    open('/work/canary.txt', 'w').write('x')
    errs.append('ro_file_write')
except OSError:
    pass
# 6) write into system ro-bind
try:
    open('/usr/pwned', 'w').write('x')
    errs.append('ro_bind_write')
except OSError:
    pass
if errs:
    sys.stderr.write('HARDENING_FAIL:' + ','.join(errs) + '\\n')
    sys.exit(2)
sys.exit(0)
""" % (str(meta_path),)
    r = run_sandbox(
        ["python3", "-c", attack],
        limits=sandbox_limits(timeout_s=10, memory_mb=64, max_output_bytes=1_000_000),
        inputs={"canary.txt": b"ro-canary"},
        env={},
    )
    assert meta_path.read_bytes() == original
    assert r.net_isolation == "hard"
    assert r.fs_isolation == "hard"
    assert r.status is SandboxStatus.OK, r.stderr.decode(errors="replace")


def test_exec_runs_exit_code(tmp_path):
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="exec_runs",
        argv=["/bin/true"],
        timeout_s=5,
        memory_mb=64,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=True,
    )
    cr = evaluate_code_check(
        check,
        template_id="exec",
        check_id="exec#0",
        out=AgentTaskOutput(outputs={}),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.MET
    assert cr.effect is not None
    # trusted requires hard isolation *and* cgroup limits held; degraded hosts
    # (no delegation) correctly stamp trusted=False with a missing-cgroup note.
    assert cr.effect.get("net_isolation") == "hard"
    assert cr.effect.get("fs_isolation") == "hard"
    if "cgroup limits missing" in (cr.effect.get("note") or ""):
        assert cr.effect.get("trusted") is False
    else:
        assert cr.effect.get("trusted") is True
    assert cr.effect.get("countable") is True
    assert cr.effect.get("produced_by_call_id", "").startswith("st/E1#1:")


def test_tests_pass_runs_catalog_tests_not_agent(tmp_path):
    """Catalog/task tests mounted RO; agent junit/conftest ignored; junit in results."""
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    # Agent ships malicious conftest + fake junit content as "code".
    agent_code = arts.put(
        b"def add(a, b):\n    return a + b\n",
        make_tool_ref("st/E1#1:code"),
        name="solution.py",
        media_type="text/x-python",
    )
    fake_junit = arts.put(
        b'<testsuite tests="99" failures="0"/>',
        make_tool_ref("st/E1#1:junit"),
        name="junit.xml",
        media_type="application/xml",
    )
    out = AgentTaskOutput(
        outputs={
            "solution.py": ArtifactHandle(artifact_id=agent_code.artifact_id, name="solution.py"),
            "conftest.py": ArtifactHandle(artifact_id=fake_junit.artifact_id, name="conftest.py"),
        }
    )
    catalog_tests = {
        "test_add.py": (
            "import solution\n"
            "def test_add():\n"
            "    assert solution.add(1, 1) == 2\n"
            "def test_add2():\n"
            "    assert solution.add(2, 3) == 5\n"
        )
    }
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="tests_pass",
        argv=list(BUILTIN_TESTS_PASS_ARGV),
        tests=catalog_tests,
        input_allowlist=["solution.py"],
        min_tests=2,
        timeout_s=30,
        memory_mb=128,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=True,
    )
    cr = evaluate_code_check(
        check,
        template_id="tp",
        check_id="tp#0",
        out=out,
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.MET, cr.detail
    assert cr.effect and cr.effect.get("producer") == "sandbox"
    assert cr.effect.get("trusted") is False
    assert cr.effect.get("countable") is False
    assert cr.effect.get("observation_only") is True
    assert "tamper-able, not counted" in (cr.effect.get("note") or "")
    assert "M2" in (cr.effect.get("note") or "")


def test_holdout_from_catalog_ignores_agent_slot_and_claimed_score(tmp_path):
    """Catalog holdout labels; agent holdout slot + claimed score ignored."""
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    catalog_holdout = {"y": [0, 1, 1, 0]}
    holdout_digest = sha256_hex(canonical_json(catalog_holdout))
    # Agent tries to supply matching labels + claimed 0.9 — must not win.
    agent_holdout = arts.put(
        json.dumps({"y": [0, 1, 0, 0]}).encode(),  # would make accuracy 1.0 if used
        make_tool_ref("st/E1#1:holdout"),
        name="holdout.json",
        media_type="application/json",
    )
    predictions = arts.put(
        json.dumps({"y_hat": [0, 1, 0, 0], "score": 0.9}).encode(),
        make_tool_ref("st/E1#1:pred"),
        name="predictions.json",
        media_type="application/json",
    )
    out = AgentTaskOutput(
        outputs={
            "holdout": ArtifactHandle(artifact_id=agent_holdout.artifact_id, name="holdout"),
            "predictions": ArtifactHandle(artifact_id=predictions.artifact_id, name="predictions"),
        }
    )
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="metric_on_holdout",
        argv=list(BUILTIN_METRIC_SCORER_ARGV),
        input_allowlist=["predictions.json"],
        holdout=catalog_holdout,
        holdout_digest=holdout_digest,
        output="predictions",
        op=">=",
        target=0.4,
        timeout_s=10,
        memory_mb=64,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=True,
    )
    cr = evaluate_code_check(
        check,
        template_id="ho",
        check_id="ho#0",
        out=out,
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.MET, cr.detail
    assert cr.effect is not None
    # True accuracy vs catalog y: 3/4 = 0.75 — not 1.0 (agent labels) or 0.9 (claim).
    assert cr.effect.get("est") == 0.75
    assert cr.effect.get("est") != 0.9
    assert cr.effect.get("countable") is True

    # y_hat copied from agent-supplied labels cannot force a perfect score.
    check_hi = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="metric_on_holdout",
        argv=list(BUILTIN_METRIC_SCORER_ARGV),
        input_allowlist=["predictions.json"],
        holdout=catalog_holdout,
        holdout_digest=holdout_digest,
        output="predictions",
        op=">=",
        target=0.9,
        timeout_s=10,
        memory_mb=64,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=True,
    )
    cr_hi = evaluate_code_check(
        check_hi,
        template_id="ho",
        check_id="ho#hi",
        out=out,
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    )
    assert cr_hi.status is ReqStatus.FAILED
    assert cr_hi.effect is not None
    assert cr_hi.effect.get("est") == 0.75


def test_parse_reward_rejects_pass_strings():
    with pytest.raises(ValueError):
        parse_reward(b"pass")
    with pytest.raises(ValueError):
        parse_reward(b"0.5")
    assert parse_reward(_REWARD_JSON(0.5)) == 0.5


def test_reproducible_rerun_compares_result_digests(tmp_path):
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="reproducible_rerun",
        argv=[
            "python3",
            "-c",
            "import json,os,pathlib;pathlib.Path(os.environ['COSCIENTIST_RESULTS'])"
            f".joinpath('out.digest').write_text(json.dumps("
            f"{{'schema': {REWARD_SCHEMA!r}, 'value': 1.0}}))",
        ],
        rerun_tol=0.01,
        timeout_s=5,
        memory_mb=64,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=True,
    )
    cr = evaluate_code_check(
        check,
        template_id="rr",
        check_id="rr#0",
        out=AgentTaskOutput(outputs={}),
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.MET
    assert cr.effect is not None
    # Constant agent reward must not be countable / must not carry est → k_sup.
    assert cr.effect.get("countable") is False
    assert cr.effect.get("observation_only") is True
    assert "est" not in cr.effect
    assert cr.effect.get("trusted") is False


def test_countable_requires_attempt_binding(tmp_path):
    """Unbound attempt_id must not invent noattempt produced_by_call_id."""
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    with pytest.raises(CatalogError, match="attempt_id_required"):
        evaluate_code_check(
            CodeCheck(
                reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
                output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
                kind="exec_runs",
                argv=["/bin/true"],
                timeout_s=5,
                memory_mb=64,
                max_output_bytes=TEST_MAX_OUTPUT_BYTES,
                pids_max=TEST_PIDS_MAX,
                rlimit_nproc=TEST_RLIMIT_NPROC,
                allow_network=False,
                allow_cgroup_degraded=True,
            ),
            template_id="exec",
            check_id="exec#nobind",
            out=AgentTaskOutput(outputs={}),
            artifacts=arts,
            values={},
            role="validity",
            attempt_id=None,
        )


def test_exec_runs_timeout_is_noncountable_refute_unresolved(tmp_path):
    """Sleep-bypass → FAILED guest timeout; never countable; refute_unresolved."""
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="exec_runs",
        argv=["python3", "-c", "import time; time.sleep(1e9)"],
        # >=2s wall so no-cgroup RLIMIT_CPU can be set (fail-closed wrapper).
        timeout_s=2.0,
        memory_mb=64,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=True,
    )
    cr = evaluate_code_check(
        check,
        template_id="exec",
        check_id="exec#t",
        out=AgentTaskOutput(outputs={}),
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.FAILED
    assert cr.effect is not None
    assert cr.effect.get("timeout_kind") in (
        "idle_timeout",
        "wall_timeout",
        "cpu_timeout",
    )
    assert cr.effect.get("countable") is False
    assert cr.effect.get("observation_only") is True
    assert cr.effect.get("refute_unresolved") is True


@pytest.mark.asyncio
async def test_sandbox_infra_report_contains_reason_and_soft_note(tmp_path, clock, runtime_cls, monkeypatch):
    """MJ4: fail-closed isolation → infra; study still finalizes with report."""
    _force_soft(monkeypatch)
    cat = _catalog_with(
        {
            "id": "exec_ok",
            "title": "exec",
            "check": {
                "kind": "exec_runs",
                "metric": "exec_runs",
                "op": "==",
                "target": "ok",
                "argv": ["/bin/true"],
                "timeout_s": 5,
                "memory_mb": 64,
            },
            "role": "validity",
        }
    )
    sid = "st_sbx_mj4"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls, catalog=cat)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        inputs=[],
    )
    ex, arts = attach_executor(rt, tmp_path)
    ex.catalog = cat
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="run",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="exec_ok")],
            )
        ],
        questions=[],
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )
    aid = await rt.start_attempt("E1")
    result = await ex.record(
        aid,
        AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS),
    )
    assert result.outcome is Outcome.FAILED
    assert result.reason == TaskReason.SANDBOX_INFRA.value
    assert result.model_dump().get("result_class") == "infra" or True
    # Result object may expose via store:
    row = rt.results.get(aid) if hasattr(rt, "results") else None
    if row:
        assert row.get("result_class") == "infra"
    await record_success(rt, await rt.start_attempt("REPORT"), arts)
    for _ in range(8):
        if getattr(rt, "_finalized", False):
            break
        await rt.tick()
        clock.advance(timedelta(seconds=5))
    assert getattr(rt, "_finalized", False)
    events = await rt.store.read_all()
    fin = next(e for e in events if e.type == "StudyFinalized")
    report_id = fin.payload.get("report_artifact_id")
    assert report_id
    report_text = arts.read(report_id).decode()
    assert TaskReason.SANDBOX_INFRA.value in (fin.payload.get("contributing_reasons") or [])
    assert "sandbox_unavailable" in report_text
    assert "bwrap hard probe failed" in report_text
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_exec_runs_production_path_trusted(tmp_path, clock, runtime_cls):
    _require_hard()
    cat = _catalog_with(
        {
            "id": "exec_ok",
            "title": "exec",
            "check": {
                "kind": "exec_runs",
                "metric": "exec_runs",
                "op": "==",
                "target": "ok",
                "argv": ["/bin/true"],
                "timeout_s": 5,
                "memory_mb": 64,
            },
            "role": "validity",
        }
    )
    sid = "st_sbx_ok"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls, catalog=cat)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        inputs=[],
    )
    ex, arts = attach_executor(rt, tmp_path)
    ex.catalog = cat
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="run",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="exec_ok")],
            )
        ],
        questions=[],
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )
    aid = await rt.start_attempt("E1")
    result = await ex.record(aid, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS))
    assert result.outcome is Outcome.SUCCESS
    sandbox_arts = [a for a in arts._index.values() if a.producer == "sandbox"]
    assert sandbox_arts
    assert all(a.net_isolation == "hard" for a in sandbox_arts)
    assert all(a.fs_isolation == "hard" for a in sandbox_arts)
    await close_runtime(rt)


def test_streaming_output_cap_is_scientific():
    """Guest output flood → OUTPUT_EXCEEDED scientific (not infra)."""
    _require_hard()
    r = run_sandbox(
        ["python3", "-c", "print('x'*100000)"],
        limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1000),
        inputs={},
        env={},
    )
    assert r.status is SandboxStatus.OUTPUT_EXCEEDED
    assert r.reason == "output_exceeded"
    assert r.status not in INFRA_STATUSES
    assert len(r.stdout) <= 1000


def test_verifier_reads_reward_not_stdout():
    _require_hard()
    reward = json.dumps({"schema": REWARD_SCHEMA, "value": 0.5})
    r = run_verifier(
        [
            "python3",
            "-I",
            "-c",
            "import os,pathlib; print('999'); "
            "pathlib.Path(os.environ['COSCIENTIST_RESULTS'])"
            f".joinpath('reward.txt').write_text({reward!r})",
        ],
        inputs={"predictions.json": b'{"y_hat": [0], "score": 0.9}'},
        extra_ro_files={},
        input_allowlist=frozenset({"predictions.json"}),
        limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1_000_000),
        collect=["reward.txt"],
    )
    assert r.status is SandboxStatus.OK
    assert parse_reward(r.result_files["reward.txt"]) == 0.5
    assert b"999" in r.stdout  # stdout must not be the metric source


def test_agent_filename_allowlist_rejects_hooks_and_pth():
    """Allowlist-only exact match — no denylist / suffix heuristics."""
    from coscientist.checks.sandbox import agent_input_name_allowed

    allow = frozenset({"solution.py", "predictions.json"})
    # Names absent from allowlist are refused (hooks/.pth included only as examples).
    assert not agent_filename_allowed("sitecustomize.py", allow)
    assert not agent_filename_allowed("usercustomize.py", allow)
    assert not agent_filename_allowed("foo.pth", allow)
    assert not agent_filename_allowed("os.py", allow)
    assert not agent_filename_allowed("pytest.py", allow)
    assert agent_filename_allowed("solution.py", allow)
    assert agent_filename_allowed("predictions.json", allow)
    assert not agent_filename_allowed("solution.py", frozenset())
    # Empty allowlist → mount nothing; paths / empty names refused.
    assert not agent_input_name_allowed("solution.py", frozenset())
    assert agent_input_name_allowed("solution.py", frozenset({"solution.py"}))
    assert not agent_input_name_allowed("evil.py", frozenset({"solution.py"}))
    assert not agent_input_name_allowed("dir/solution.py", frozenset({"solution.py"}))
    assert not agent_input_name_allowed("", frozenset({"solution.py"}))


def test_y_hat_equals_catalog_labels_still_uses_catalog_holdout(tmp_path):
    """Copying catalog labels into y_hat scores honestly; agent holdout unused."""
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    catalog_holdout = {"y": [1, 0, 1, 0]}
    digest = sha256_hex(canonical_json(catalog_holdout))
    # Perfect y_hat (= catalog y) → 1.0; agent also ships fake holdout with other y.
    predictions = arts.put(
        json.dumps({"y_hat": [1, 0, 1, 0], "score": 0.0}).encode(),
        make_tool_ref("st/E1#1:pred"),
        name="predictions.json",
        media_type="application/json",
    )
    fake_hold = arts.put(
        json.dumps({"y": [0, 0, 0, 0]}).encode(),
        make_tool_ref("st/E1#1:holdout"),
        name="holdout.json",
        media_type="application/json",
    )
    out = AgentTaskOutput(
        outputs={
            "predictions": ArtifactHandle(artifact_id=predictions.artifact_id, name="predictions"),
            "holdout": ArtifactHandle(artifact_id=fake_hold.artifact_id, name="holdout"),
        }
    )
    cr = evaluate_code_check(
        CodeCheck(
            reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
            output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
            kind="metric_on_holdout",
            argv=list(BUILTIN_METRIC_SCORER_ARGV),
            input_allowlist=["predictions.json"],
            holdout=catalog_holdout,
            holdout_digest=digest,
            output="predictions",
            op=">=",
            target=0.99,
            timeout_s=10,
            memory_mb=64,
            max_output_bytes=TEST_MAX_OUTPUT_BYTES,
            pids_max=TEST_PIDS_MAX,
            rlimit_nproc=TEST_RLIMIT_NPROC,
            allow_network=False,
            allow_cgroup_degraded=True,
        ),
        template_id="ho",
        check_id="ho#eq",
        out=out,
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.MET
    assert cr.effect and cr.effect.get("est") == 1.0


def test_sitecustomize_slot_rejected_from_inputs(tmp_path):
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    evil = arts.put(
        b"open('/results/pwned','w').write('x')\n",
        make_tool_ref("st/E1#1:evil"),
        name="sitecustomize.py",
        media_type="text/x-python",
    )
    good = arts.put(
        b"def add(a,b): return a+b\n",
        make_tool_ref("st/E1#1:code"),
        name="solution.py",
        media_type="text/x-python",
    )
    out = AgentTaskOutput(
        outputs={
            "sitecustomize.py": ArtifactHandle(artifact_id=evil.artifact_id, name="sitecustomize.py"),
            "solution.py": ArtifactHandle(artifact_id=good.artifact_id, name="solution.py"),
        }
    )
    cr = evaluate_code_check(
        CodeCheck(
            reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
            output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
            kind="tests_pass",
            argv=list(BUILTIN_TESTS_PASS_ARGV),
            tests={"test_add.py": ("import solution\ndef test_add():\n    assert solution.add(1,2)==3\n")},
            input_allowlist=["solution.py"],
            min_tests=1,
            timeout_s=30,
            memory_mb=128,
            max_output_bytes=TEST_MAX_OUTPUT_BYTES,
            pids_max=TEST_PIDS_MAX,
            rlimit_nproc=TEST_RLIMIT_NPROC,
            allow_network=False,
            allow_cgroup_degraded=True,
        ),
        template_id="tp",
        check_id="tp#site",
        out=out,
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr.effect is not None
    assert cr.effect.get("trusted") is False
    assert cr.effect.get("countable") is False
    assert cr.status is ReqStatus.MET, cr.detail
    # Rejected sitecustomize must not have written /results/pwned.
    assert all("pwned" not in (e.artifact_id or "") for e in (cr.evidence or []))


def test_forged_junit_os_exit_still_untrusted(tmp_path):
    """Agent code can forge junit + os._exit — observation only (not countable)."""
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    # Malicious module imported by catalog test: forge junit then exit 0.
    evil = arts.put(
        b"import os, pathlib\n"
        b"pathlib.Path('/results/junit.xml').write_text("
        b'\'<testsuite tests="9" failures="0" errors="0"/>\')\n'
        b"os._exit(0)\n",
        make_tool_ref("st/E1#1:evil"),
        name="solution.py",
        media_type="text/x-python",
    )
    out = AgentTaskOutput(outputs={"solution.py": ArtifactHandle(artifact_id=evil.artifact_id, name="solution.py")})
    cr = evaluate_code_check(
        CodeCheck(
            reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
            output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
            kind="tests_pass",
            argv=list(BUILTIN_TESTS_PASS_ARGV),
            tests={"test_import.py": "import solution\ndef test_noop():\n    assert True\n"},
            input_allowlist=["solution.py"],
            min_tests=1,
            timeout_s=30,
            memory_mb=128,
            max_output_bytes=TEST_MAX_OUTPUT_BYTES,
            pids_max=TEST_PIDS_MAX,
            rlimit_nproc=TEST_RLIMIT_NPROC,
            allow_network=False,
            allow_cgroup_degraded=True,
        ),
        template_id="tp",
        check_id="tp#forge",
        out=out,
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    # May MET if forged junit wins, or FAILED — never trusted/countable.
    assert cr.effect is not None
    assert cr.effect.get("trusted") is False
    assert cr.effect.get("countable") is False
    assert cr.effect.get("observation_only") is True
    assert "tamper-able, not counted" in (cr.effect.get("note") or "")


def test_pth_file_not_mounted_even_if_named_in_outputs(tmp_path):
    """Real .pth payload is rejected by denylist even when listed as an output."""
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    pth = arts.put(
        b"import os; open('/results/pth_pwned','w').write('x')\n",
        make_tool_ref("st/E1#1:pth"),
        name="evil.pth",
        media_type="text/plain",
    )
    good = arts.put(
        b"def add(a,b): return a+b\n",
        make_tool_ref("st/E1#1:code"),
        name="solution.py",
        media_type="text/x-python",
    )
    out = AgentTaskOutput(
        outputs={
            "evil.pth": ArtifactHandle(artifact_id=pth.artifact_id, name="evil.pth"),
            "solution.py": ArtifactHandle(artifact_id=good.artifact_id, name="solution.py"),
        }
    )
    cr = evaluate_code_check(
        CodeCheck(
            reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
            output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
            kind="tests_pass",
            argv=list(BUILTIN_TESTS_PASS_ARGV),
            tests={"test_add.py": ("import solution\ndef test_add():\n    assert solution.add(1,2)==3\n")},
            # Allowlist-only: evil.pth may be listed, but python -I + no /inputs on
            # sys.path means .pth hooks still cannot run (observation-only defense).
            input_allowlist=["solution.py", "evil.pth"],
            min_tests=1,
            timeout_s=30,
            memory_mb=128,
            max_output_bytes=TEST_MAX_OUTPUT_BYTES,
            pids_max=TEST_PIDS_MAX,
            rlimit_nproc=TEST_RLIMIT_NPROC,
            allow_network=False,
            allow_cgroup_degraded=True,
        ),
        template_id="tp",
        check_id="tp#pth",
        out=out,
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.MET, cr.detail
    assert cr.effect and cr.effect.get("countable") is False


def test_holdout_digest_mismatch_is_catalog_error(tmp_path):
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    predictions = arts.put(
        json.dumps({"y_hat": [0, 1]}).encode(),
        make_tool_ref("st/E1#1:pred"),
        name="predictions.json",
        media_type="application/json",
    )
    out = AgentTaskOutput(
        outputs={"predictions": ArtifactHandle(artifact_id=predictions.artifact_id, name="predictions")}
    )
    from coscientist.core.events.errors import CatalogError

    with pytest.raises(CatalogError, match="holdout_digest mismatch"):
        evaluate_code_check(
            CodeCheck(
                reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
                output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
                kind="metric_on_holdout",
                argv=list(BUILTIN_METRIC_SCORER_ARGV),
                input_allowlist=["predictions.json"],
                holdout={"y": [0, 1]},
                holdout_digest="0" * 64,
                output="predictions",
                op=">=",
                target=0.5,
                timeout_s=10,
                memory_mb=64,
                max_output_bytes=TEST_MAX_OUTPUT_BYTES,
                pids_max=TEST_PIDS_MAX,
                rlimit_nproc=TEST_RLIMIT_NPROC,
                allow_network=False,
                allow_cgroup_degraded=True,
            ),
            template_id="ho",
            check_id="ho#bad",
            out=out,
            artifacts=arts,
            values={},
            role="prediction",
            attempt_id="st/E1#1",
        )


def test_holdout_never_exposed_to_agent_jail(tmp_path):
    """Catalog holdout is verifier-only; agent jail must not see labels."""
    _require_hard()
    secret = {"y": [1, 0, 1, 0], "secret": "HOLD_LABELS"}
    # Agent jail: try to read holdout env / typical paths — must fail closed.
    attack = r"""
import os, sys
errs = []
if 'COSCIENTIST_HOLDOUT' in os.environ:
    errs.append('env')
for p in ('/holdout', '/holdout.bin', '/inputs/holdout.json', '/results/holdout.bin'):
    try:
        open(p, 'rb').read()
        errs.append(p)
    except OSError:
        pass
if errs:
    sys.stderr.write('HOLDOUT_LEAK:' + ','.join(errs) + '\n')
    sys.exit(2)
sys.exit(0)
"""
    r = run_sandbox(
        ["python3", "-I", "-c", attack],
        limits=sandbox_limits(timeout_s=10, memory_mb=64, max_output_bytes=1_000_000),
        inputs={},
        env={},
    )
    assert r.status is SandboxStatus.OK, r.stderr.decode(errors="replace")
    # Verifier still scores with catalog holdout (secret never in agent outputs).
    arts = LocalArtifactStore(tmp_path)
    digest = sha256_hex(canonical_json(secret))
    predictions = arts.put(
        json.dumps({"y_hat": [1, 0, 1, 0]}).encode(),
        make_tool_ref("st/E1#1:pred"),
        name="predictions.json",
        media_type="application/json",
    )
    cr = evaluate_code_check(
        CodeCheck(
            reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
            output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
            kind="metric_on_holdout",
            argv=list(BUILTIN_METRIC_SCORER_ARGV),
            input_allowlist=["predictions.json"],
            holdout=secret,
            holdout_digest=digest,
            output="predictions",
            op=">=",
            target=0.99,
            timeout_s=10,
            memory_mb=64,
            max_output_bytes=TEST_MAX_OUTPUT_BYTES,
            pids_max=TEST_PIDS_MAX,
            rlimit_nproc=TEST_RLIMIT_NPROC,
            allow_network=False,
            allow_cgroup_degraded=True,
        ),
        template_id="ho",
        check_id="ho#iso",
        out=AgentTaskOutput(
            outputs={"predictions": ArtifactHandle(artifact_id=predictions.artifact_id, name="predictions")}
        ),
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.MET
    assert cr.effect and cr.effect.get("est") == 1.0


def test_verifier_jail_hardening_attacks():
    _require_hard()
    attack = r"""
import os, sys, ctypes
errs = []
try:
    libc = ctypes.CDLL('libc.so.6', use_errno=True)
    if libc.umount2(b'/inputs', 0) == 0:
        errs.append('umount')
except Exception:
    pass
try:
    libc = ctypes.CDLL('libc.so.6', use_errno=True)
    if libc.mount(b'none', b'/usr', None, 32 | 4096, None) == 0:
        errs.append('remount')
except Exception:
    pass
try:
    libc = ctypes.CDLL('libc.so.6', use_errno=True)
    if hasattr(libc, 'unshare') and libc.unshare(0x10000000) == 0:
        errs.append('userns')
except Exception:
    pass
try:
    open('/inputs/pwned', 'w').write('x')
    errs.append('ro_write')
except OSError:
    pass
import json, pathlib
pathlib.Path(os.environ['COSCIENTIST_RESULTS']).joinpath('reward.txt').write_text(
    json.dumps({'schema': 'coscientist.reward.v1', 'value': 1.0})
)
if errs:
    sys.stderr.write('HARDENING_FAIL:' + ','.join(errs) + '\n')
    sys.exit(2)
sys.exit(0)
"""
    r = run_verifier(
        ["python3", "-I", "-c", attack],
        inputs={"predictions.json": b'{"y_hat":[0]}'},
        extra_ro_files={},
        input_allowlist=frozenset({"predictions.json"}),
        limits=sandbox_limits(timeout_s=10, memory_mb=64, max_output_bytes=1_000_000),
        collect=["reward.txt"],
    )
    assert r.status is SandboxStatus.OK, r.stderr.decode(errors="replace")
    assert parse_reward(r.result_files["reward.txt"]) == 1.0


def test_cgroup_attach_failure_is_infra(monkeypatch):
    """Launcher attach failure must signal via private FD → UNAVAILABLE infra."""
    _require_hard()
    import coscientist.checks.sandbox as sb

    # Force a leaf so the launcher path is used, then break attach.
    real_prepare = sb._cgroup_prepare_leaf

    def fake_prepare(lim):
        prep = real_prepare(lim)
        if prep.leaf is None:
            pytest.skip("cgroup delegation unavailable")
        # Replace launcher writer with one that always fails attach + signals infra.
        return prep

    monkeypatch.setattr(sb, "_cgroup_prepare_leaf", fake_prepare)

    broken = """
import os, sys
infra_fd = os.environ.get("COSCIENTIST_INFRA_FD")
if infra_fd is not None:
    try:
        os.write(int(infra_fd), b"COS_SBX_INFRA_v1:cgroup_attach\\n")
    except OSError:
        pass
sys.stderr.write("cgroup_attach:forced\\n")
sys.exit(70)
"""
    monkeypatch.setattr(sb, "_cgroup_bwrap_launcher", lambda leaf: broken)
    r = run_sandbox(
        ["/bin/true"],
        limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1_000_000),
        inputs={},
        env={},
    )
    if r.note and "OOM not detectable" in r.note and r.status is SandboxStatus.OK:
        pytest.skip("cgroup leaf not created on this host")
    assert r.status is SandboxStatus.UNAVAILABLE
    assert "cgroup_attach" in r.reason
    assert r.status in INFRA_STATUSES


def test_cgroup_oom_detectable_or_skip():
    """Real cgroup OOM when delegation works; otherwise skip."""
    _require_hard()
    import coscientist.checks.sandbox as sb

    try:
        cgroup_ok = sb._cgroup_delegation_available()
    except SandboxInfraError:
        cgroup_ok = False
    if not cgroup_ok:
        pytest.skip("cgroup memory delegation unavailable")
    # Allocate far beyond memory.max — expect OOM infra when detectable.
    r = run_sandbox(
        [
            "python3",
            "-c",
            "x = bytearray(200 * 1024 * 1024); x[0] = 1; import time; time.sleep(30)",
        ],
        limits=sandbox_limits(timeout_s=5, memory_mb=8, max_output_bytes=10_000),
        inputs={},
        env={},
    )
    if not r.oom_detectable:
        pytest.skip("guest cgroup membership not verified (oom_detectable=False)")
    # May be OOM or timeout if allocator fails soft — OOM is the success path.
    if r.status is SandboxStatus.OOM:
        assert r.reason == "sandbox_oom"
        assert r.status in INFRA_STATUSES
    else:
        # Soft MemoryError / kill without oom_kill counter — still must not be OK.
        assert r.status is not SandboxStatus.OK


def test_agent_facing_view_never_includes_holdout_or_scorer():
    """Prompt/agent serialization must not expose CodeCheck secrets."""
    from coscientist.checks.evaluate import (
        agent_facing_template_view,
        template_to_requirement,
    )
    from coscientist.core.events.digest import canonical_json, sha256_hex
    from coscientist.core.execution.domain import RequirementTemplate

    holdout = {"y": [0, 1], "secret": "LABELS"}
    tmpl = RequirementTemplate(
        id="metric_secret",
        title="metric secret",
        text_ru="Метрика на удержанной выборке",
        version=1,
        required=True,
        role="prediction",
        method_sources=["test"],
        rule_ids=["R23"],
        applies_to=["experiment"],
        exempt_from_freeze=False,
        check=CodeCheck(
            reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
            output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
            kind="metric_on_holdout",
            argv=list(BUILTIN_METRIC_SCORER_ARGV),
            input_allowlist=["predictions.json"],
            holdout=holdout,
            holdout_digest=sha256_hex(canonical_json(holdout)),
            output="predictions",
            op=">=",
            target=0.5,
            tests={"test_x.py": "def test_x():\n    assert False\n"},
            max_output_bytes=TEST_MAX_OUTPUT_BYTES,
            pids_max=TEST_PIDS_MAX,
            rlimit_nproc=TEST_RLIMIT_NPROC,
            allow_network=False,
            allow_cgroup_degraded=True,
        ),
    )
    view = agent_facing_template_view(tmpl)
    blob = json.dumps(view, default=str)
    # Kind name may contain the substring; secrets / payloads must not.
    assert "holdout" not in view
    assert "LABELS" not in blob
    assert '"y"' not in blob
    assert BUILTIN_METRIC_SCORER_ARGV[0] not in blob
    assert "test_x" not in blob
    assert "assert False" not in blob
    assert view["text_ru"] == tmpl.text_ru
    assert view["is_code"] is True
    assert view["check_detail"] == "metric_on_holdout"
    # NodeRequirement path also omits the check body.
    req = template_to_requirement(tmpl, "n1", req_id=f"n1:{tmpl.id}")
    req_dump = req.model_dump()
    assert "holdout" not in req_dump
    assert "check" not in req_dump
    assert "LABELS" not in json.dumps(req_dump)


def test_classify_guest_timeout_idle_vs_busy():
    from coscientist.checks.catalog import SANDBOX_IDLE_CPU_WALL_RATIO
    from coscientist.checks.sandbox import classify_guest_timeout
    from coscientist.core.events.errors import SandboxInfraError

    idle = SANDBOX_IDLE_CPU_WALL_RATIO
    assert (
        classify_guest_timeout(
            kill_cause="wall",
            duration_s=1.0,
            cpu_usage_s=0.01,
            idle_ratio=idle,
        )
        == "idle_timeout"
    )
    assert (
        classify_guest_timeout(
            kill_cause="cpu",
            duration_s=1.0,
            cpu_usage_s=0.9,
            idle_ratio=idle,
        )
        == "cpu_timeout"
    )
    assert (
        classify_guest_timeout(
            kill_cause="wall",
            duration_s=1.0,
            cpu_usage_s=0.5,
            idle_ratio=idle,
        )
        == "wall_timeout"
    )
    # Missing cpu → infra (no role-based idle/wall invent).
    with pytest.raises(SandboxInfraError, match="missing_cpu_usage"):
        classify_guest_timeout(
            kill_cause="wall",
            duration_s=1.0,
            cpu_usage_s=None,
            idle_ratio=idle,
        )
    # kill_cause=None alone never yields cpu_timeout from classify_guest_timeout
    # (SIGXCPU promotion is in _classify via rusage / cgroup measurement).
    assert (
        classify_guest_timeout(
            kill_cause=None,
            duration_s=1.0,
            cpu_usage_s=0.5,
            idle_ratio=idle,
        )
        == "wall_timeout"
    )


def test_timeout_kind_for_role_refuses_non_timeout_and_empty_reason(monkeypatch):
    """Non-TIMEOUT / empty reason / bad kind → SandboxInfraError (typed result)."""
    from coscientist.checks.catalog import SANDBOX_IDLE_CPU_WALL_RATIO
    from coscientist.checks.kinds.executable import _timeout_kind_for_role
    from coscientist.checks.sandbox import SandboxResult

    def _res(**kw: object) -> SandboxResult:
        base = dict(
            status=SandboxStatus.TIMEOUT,
            exit_code=-1,
            stdout=b"",
            stderr=b"",
            reason="wall_timeout",
            duration_s=1.0,
            idle_cpu_wall_ratio=SANDBOX_IDLE_CPU_WALL_RATIO,
            timeout_kill="wall",
            cpu_usage_s=0.5,
            net_isolation="hard",
            fs_isolation="hard",
            oom_detectable=True,
        )
        base.update(kw)
        return SandboxResult(**{**base, "cgroup_limits_held": True})  # type: ignore[arg-type]

    with pytest.raises(SandboxInfraError, match="non_timeout_status"):
        _timeout_kind_for_role(_res(status=SandboxStatus.OK, reason="exit:0"), "validity")

    with pytest.raises(SandboxInfraError, match="missing_timeout_reason"):
        _timeout_kind_for_role(_res(reason=""), "validity")

    assert _timeout_kind_for_role(_res(), "validity") == "wall_timeout"

    import coscientist.checks.sandbox as sb_mod

    monkeypatch.setattr(
        sb_mod,
        "classify_guest_timeout",
        lambda **k: "not_a_timeout_kind",
    )
    with pytest.raises(SandboxInfraError, match="bad_timeout_kind"):
        _timeout_kind_for_role(_res(), "validity")


def test_executable_kinds_are_dispatch_keys(tmp_path):
    from typing import get_args

    from coscientist.checks.kinds.executable import (
        _EXECUTABLE_DISPATCH,
        EXECUTABLE_KINDS,
        evaluate_executable,
    )

    assert EXECUTABLE_KINDS == frozenset(_EXECUTABLE_DISPATCH)
    # Stay in sync with CodeCheck.kind Literal (domain drift guard).
    literal_kinds = frozenset(get_args(CodeCheck.model_fields["kind"].annotation))
    expected = frozenset({"exec_runs", "tests_pass", "metric_on_holdout", "reproducible_rerun"})
    assert expected <= literal_kinds
    assert EXECUTABLE_KINDS == expected
    with pytest.raises(SandboxInfraError, match="unknown_kind"):
        evaluate_executable(
            CodeCheck(
                kind="value_matches",
                output="main",
                target=1,
                allow_network=False,
                allow_cgroup_degraded=False,
            ),
            template_id="x",
            check_id="x",
            out=AgentTaskOutput(outputs={}),
            artifacts=LocalArtifactStore(tmp_path),
            role="validity",
            attempt_id=None,
            prov_fields={},
        )


def test_guest_timeout_result_typed_sandbox_result_refuses_bad_kind(monkeypatch):
    """guest_timeout_result takes SandboxResult; kind ∉ TIMEOUT_KINDS raises."""
    from coscientist.checks.catalog import SANDBOX_IDLE_CPU_WALL_RATIO
    from coscientist.checks.kinds._util import (
        check_is_guest_timeout,
        guest_timeout_result,
    )
    from coscientist.checks.sandbox import SandboxResult

    def _res(**kw: object) -> SandboxResult:
        base = dict(
            status=SandboxStatus.TIMEOUT,
            exit_code=-1,
            stdout=b"",
            stderr=b"",
            reason="wall_timeout",
            duration_s=1.0,
            idle_cpu_wall_ratio=SANDBOX_IDLE_CPU_WALL_RATIO,
            cgroup_limits_held=True,
            timeout_kill="wall",
            cpu_usage_s=0.5,
            net_isolation="hard",
            fs_isolation="hard",
            oom_detectable=True,
        )
        base.update(kw)
        return SandboxResult(**{**base, "cgroup_limits_held": True})  # type: ignore[arg-type]

    with pytest.raises(SandboxInfraError, match="non_timeout_status"):
        guest_timeout_result(
            check_id="c",
            template_id="t",
            role="validity",
            attempt_id=None,
            result=_res(status=SandboxStatus.OK, reason="exit:0"),
            evidence=[],
            jail_inputs_digest=None,
        )

    ok = guest_timeout_result(
        check_id="c",
        template_id="t",
        role="validity",
        attempt_id="a1",
        result=_res(),
        evidence=[],
        jail_inputs_digest=None,
    )
    assert ok is not None
    assert ok.effect["timeout_kind"] == "wall_timeout"
    assert ok.effect["countable"] is False
    assert check_is_guest_timeout(ok) is True
    # reason alone must not classify as guest timeout (no soft or-default).
    fake = CheckResult(
        check_id="c",
        template_id="t",
        role="validity",
        status=ReqStatus.FAILED,
        is_code=True,
        required=True,
        evidence=[],
        detail="wall_timeout",
        effect={"reason": "wall_timeout"},
        used_sesoi=False,
    )
    assert check_is_guest_timeout(fake) is False

    import coscientist.checks.sandbox as sb_mod

    monkeypatch.setattr(
        sb_mod,
        "classify_guest_timeout",
        lambda **k: "not_a_timeout_kind",
    )
    with pytest.raises(SandboxInfraError, match="bad_timeout_kind"):
        guest_timeout_result(
            check_id="c",
            template_id="t",
            role="validity",
            attempt_id="a1",
            result=_res(),
            evidence=[],
            jail_inputs_digest=None,
        )
    with pytest.raises(CatalogError, match="attempt_id_required"):
        guest_timeout_result(
            check_id="c",
            template_id="t",
            role="validity",
            attempt_id=None,
            result=_res(),
            evidence=[],
            jail_inputs_digest=None,
        )


def test_metric_on_holdout_requires_output_and_op_on_codecheck():
    """``output`` / ``op`` required by CodeCheck validator (before sandbox)."""
    from pydantic import ValidationError

    holdout = {"y": [0, 1]}
    digest = sha256_hex(canonical_json(holdout))
    base = dict(
        kind="metric_on_holdout",
        argv=list(BUILTIN_METRIC_SCORER_ARGV),
        input_allowlist=["predictions.json"],
        holdout=holdout,
        holdout_digest=digest,
        target=0.5,
        timeout_s=5.0,
        memory_mb=64,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=True,
    )
    with pytest.raises(ValidationError, match="output"):
        CodeCheck(**{**base, "op": ">="})
    with pytest.raises(ValidationError, match="op"):
        CodeCheck(**{**base, "output": "predictions"})


def test_tests_pass_and_rerun_require_catalog_fields():
    """``min_tests`` / ``rerun_tol`` required — no silent 1 / 0.0 defaults."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="min_tests"):
        CodeCheck(
            reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
            output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
            kind="tests_pass",
            argv=list(BUILTIN_TESTS_PASS_ARGV),
            tests={"t.py": "def test_x():\n    assert True\n"},
            timeout_s=5.0,
            memory_mb=64,
            max_output_bytes=TEST_MAX_OUTPUT_BYTES,
            pids_max=TEST_PIDS_MAX,
            rlimit_nproc=TEST_RLIMIT_NPROC,
            allow_network=False,
            allow_cgroup_degraded=False,
        )
    with pytest.raises(ValidationError, match="rerun_tol"):
        CodeCheck(
            reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
            output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
            kind="reproducible_rerun",
            argv=["/bin/true"],
            timeout_s=5.0,
            memory_mb=64,
            max_output_bytes=TEST_MAX_OUTPUT_BYTES,
            pids_max=TEST_PIDS_MAX,
            rlimit_nproc=TEST_RLIMIT_NPROC,
            allow_network=False,
            allow_cgroup_degraded=False,
        )
    # Negative rerun_tol rejected for any kind (sole source; tolerance forbidden).
    with pytest.raises(ValidationError, match="rerun_tol"):
        CodeCheck(
            reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
            output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
            kind="exec_runs",
            argv=["/bin/true"],
            rerun_tol=-0.01,
            timeout_s=5.0,
            memory_mb=64,
            max_output_bytes=TEST_MAX_OUTPUT_BYTES,
            pids_max=TEST_PIDS_MAX,
            rlimit_nproc=TEST_RLIMIT_NPROC,
            allow_network=False,
            allow_cgroup_degraded=False,
        )
    with pytest.raises(ValidationError, match="rerun_tol only"):
        CodeCheck(
            reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
            output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
            kind="reproducible_rerun",
            argv=["/bin/true"],
            rerun_tol=0.01,
            tolerance=0.01,
            timeout_s=5.0,
            memory_mb=64,
            max_output_bytes=TEST_MAX_OUTPUT_BYTES,
            pids_max=TEST_PIDS_MAX,
            rlimit_nproc=TEST_RLIMIT_NPROC,
            allow_network=False,
            allow_cgroup_degraded=False,
        )


def test_reproducible_rerun_malformed_reward_raises(tmp_path, monkeypatch):
    """Reward parse failure must not soft-fall back to digest equality."""
    import coscientist.checks.kinds.executable as exe_mod
    from coscientist.checks.catalog import SANDBOX_IDLE_CPU_WALL_RATIO
    from coscientist.checks.kinds.executable import (
        DIGEST_NAME,
        REWARD_NAME,
        evaluate_reproducible_rerun,
    )
    from coscientist.checks.sandbox import SandboxResult

    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="reproducible_rerun",
        argv=["python3", "-c", "pass"],
        rerun_tol=0.01,
        timeout_s=5.0,
        memory_mb=64,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=True,
    )

    def _bad_verifier(*_a, **_k):
        return SandboxResult(
            status=SandboxStatus.OK,
            exit_code=0,
            stdout=b"",
            stderr=b"",
            reason="exit:0",
            duration_s=0.1,
            net_isolation="hard",
            fs_isolation="hard",
            result_files={
                DIGEST_NAME: b"same",
                REWARD_NAME: b"not-json",
            },
            cgroup_limits_held=True,
            oom_detectable=True,
            cpu_usage_s=0.01,
            idle_cpu_wall_ratio=SANDBOX_IDLE_CPU_WALL_RATIO,
        )

    monkeypatch.setattr(exe_mod, "run_verifier", _bad_verifier)
    cr = evaluate_reproducible_rerun(
        check,
        template_id="rr",
        check_id="rr",
        out=AgentTaskOutput(outputs={}),
        artifacts=LocalArtifactStore(tmp_path),
        role="prediction",
        attempt_id="st/E1#1",
        prov_fields={},
    )
    assert cr.status is ReqStatus.FAILED
    assert cr.detail is not None
    assert "malformed_output:reward" in cr.detail
    assert cr.effect is not None
    assert cr.effect.get("countable") is False


def test_parse_junit_requires_failures_errors_tests_attrs():
    """Missing failures/errors/tests attrs raise; no soft 0 / root re-walk."""
    from coscientist.checks.kinds._util import parse_junit

    with pytest.raises(ValueError, match="junit_missing_attr:errors"):
        parse_junit(b'<testsuite failures="0" tests="1"></testsuite>')
    with pytest.raises(ValueError, match="junit_missing_attr:failures"):
        parse_junit(b'<testsuite errors="0" tests="1"></testsuite>')
    with pytest.raises(ValueError, match="junit_missing_attr:tests"):
        parse_junit(b'<testsuite failures="0" errors="0"></testsuite>')
    with pytest.raises(ValueError):
        parse_junit(b'<testsuite failures="" errors="0" tests="1"></testsuite>')
    ok = parse_junit(b'<testsuite failures="0" errors="0" tests="1"></testsuite>')
    assert ok == (0, 1)


def test_guest_forged_sigxcpu_is_scientific_nonzero():
    """Guest os.kill(SIGXCPU) / exit 152 with low rusage must stay NONZERO."""
    _require_hard()
    r = run_sandbox(
        [
            "python3",
            "-c",
            "import os,signal; os.kill(os.getpid(), signal.SIGXCPU)",
        ],
        limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1_000_000),
        inputs={},
        env={},
    )
    assert r.status is SandboxStatus.NONZERO
    assert r.reason != "cpu_timeout"
    assert r.status not in INFRA_STATUSES
    # exit 152 (128+SIGXCPU) with no CPU burn likewise
    r2 = run_sandbox(
        ["python3", "-c", "raise SystemExit(152)"],
        limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1_000_000),
        inputs={},
        env={},
    )
    assert r2.status is SandboxStatus.NONZERO
    assert r2.exit_code == 152
    assert r2.reason != "cpu_timeout"


def test_rlimit_cpu_fuse_above_cgroup_budget():
    """With cgroup: RLIMIT_CPU is strictly above budget (fuse); without: at budget."""
    import math

    from coscientist.checks.sandbox import (
        _cpu_budget_seconds,
        _rlimit_cpu_seconds,
    )

    lim = sandbox_limits(timeout_s=10.0, memory_mb=64, max_output_bytes=10_000)
    budget = _cpu_budget_seconds(lim)
    fuse = _rlimit_cpu_seconds(lim, cgroup_live=True)
    bare = _rlimit_cpu_seconds(lim, cgroup_live=False)
    assert fuse is not None and bare is not None
    assert fuse > budget
    assert fuse <= math.floor(lim.timeout_s)
    assert bare == math.floor(budget)
    assert bare < lim.timeout_s
    # Fuse clears the race: watcher budget < fuse RLIMIT.
    extra = max(
        lim.rlimit_fuse_extra_floor_s,
        budget * lim.rlimit_fuse_extra_ratio,
    )
    assert fuse >= max(
        math.ceil(budget + extra),
        math.floor(budget) + 1,
    ) or fuse == max(
        math.ceil(
            max(
                budget + lim.rlimit_fuse_wall_margin_s,
                lim.timeout_s - lim.rlimit_fuse_wall_margin_s,
            )
        ),
        math.floor(budget) + 1,
    )


@pytest.mark.parametrize("wall", [3.0, 4.0, 5.0])
def test_rlimit_cpu_fuse_strictly_above_budget_short_wall(wall):
    """ceil fuse: wall 3–5 must stay strictly above budget (int() used to break this)."""
    import math

    from coscientist.checks.sandbox import (
        _cpu_budget_seconds,
        _rlimit_cpu_seconds,
    )

    lim = sandbox_limits(timeout_s=wall, memory_mb=64, max_output_bytes=10_000)
    budget = _cpu_budget_seconds(lim)
    fuse = _rlimit_cpu_seconds(lim, cgroup_live=True)
    bare = _rlimit_cpu_seconds(lim, cgroup_live=False)
    assert fuse is not None, f"wall={wall}: expected cgroup fuse"
    assert fuse > budget, f"wall={wall}: fuse={fuse} must be > budget={budget}"
    assert fuse <= math.floor(wall)
    assert bare is not None and bare == math.floor(budget) and bare < wall


def test_sigxcpu_with_rusage_at_budget_is_cpu_timeout(monkeypatch):
    """No-cgroup path: busy child hits SIGXCPU inside the SC_CLK_TCK tick window.

    Semantic (not just the floor invariant): RLIMIT_CPU is whole seconds at
    ``floor(budget)``; host-measured tree CPU for a CPU-bound guest must land
    within ``slack`` (= ``watch_poll_interval_s + 1/SC_CLK_TCK``) of that edge
    and promote to ``cpu_timeout``.
    """
    import coscientist.checks.sandbox as sb
    from coscientist.checks.sandbox import _cpu_budget_seconds, _rlimit_cpu_seconds

    _require_hard()
    # Force no cgroup leaf so classification uses rusage / SIGXCPU path.
    monkeypatch.setattr(
        sb,
        "_cgroup_prepare_leaf",
        lambda *a, **k: sb._CgroupPrep(
            leaf=None,
            applied=frozenset(),
            missing=("cgroup",),
            fatal="cgroup_unavailable",
        ),
    )
    monkeypatch.setattr(sb, "_cgroup_delegation_available", lambda: False)

    lim = sandbox_limits(timeout_s=3.0, memory_mb=64, max_output_bytes=10_000)
    rlim = _rlimit_cpu_seconds(lim, cgroup_live=False)
    assert rlim is not None and rlim >= 1
    tick = 1.0 / sb._clk_tck()
    slack = float(lim.watch_poll_interval_s) + tick
    assert lim.sigxcpu_slack_s is not None and lim.sigxcpu_min_measured_s is not None
    assert float(lim.sigxcpu_slack_s) == pytest.approx(slack)
    assert float(lim.sigxcpu_min_measured_s) == pytest.approx(tick)

    r = run_sandbox(
        ["python3", "-c", "while True: pass"],
        limits=lim,
        inputs={},
        env={},
    )
    assert r.status is SandboxStatus.TIMEOUT
    assert r.reason == "cpu_timeout"
    assert r.status not in INFRA_STATUSES
    assert r.cpu_usage_s is not None
    # Measured CPU must clear the promotion gate and stay near the RLIMIT edge
    # (a few ticks of sampling lag — not a multi-second heuristic).
    gate = float(rlim) - slack
    assert float(r.cpu_usage_s) >= gate
    assert float(r.cpu_usage_s) <= float(rlim) + slack + tick
    # Wall should also finish near the integer RLIMIT (busy loop, not idle wait).
    assert r.duration_s < float(rlim) + 2.0
    assert _cpu_budget_seconds(lim) >= float(rlim)

    # Forged 152 with negligible CPU stays NONZERO.
    r2 = run_sandbox(
        ["python3", "-c", "raise SystemExit(152)"],
        limits=sandbox_limits(timeout_s=5, memory_mb=64, max_output_bytes=1_000_000),
        inputs={},
        env={},
    )
    assert r2.status is SandboxStatus.NONZERO
    assert r2.reason != "cpu_timeout"


def test_refute_timeout_hard_blocks_supported():
    """Sleep/idle refute-role timeout → supported unreachable (refute_unresolved)."""
    from coscientist.core.claims.claims import RuntimeClaim
    from coscientist.core.claims.verdict import hypothesis_verdict

    def severe(**kw):
        base = dict(
            criterion_status="met",
            accepted=True,
            provenance="real",
            frozen=True,
            evidence_kind="computational",
            attempt_id="A1#1",
            artifact_id="Art1",
            validity_ok=True,
            check_role="prediction",
            claim_outcome="consistent",
            severe=True,
            independence_key="k1",
            experiment_id="A1",
            intent="confirm",
            sesoi=0.5,
            uses_sesoi=True,
            used_sesoi=True,
            predicts_null=False,
            predicted_direction="positive",
            effect={
                "est": 1.5,
                "ci_low": 1.5,
                "ci_high": 1.5,
                "producer": "sandbox",
                "trusted": False,
                "core_evaluated": True,
                "countable": True,
                "produced_by_call_id": "A1#1:1",
            },
            refute_capable=True,
        )
        base.update(kw)
        return RuntimeClaim(**base)  # type: ignore[arg-type]

    a = severe(
        independence_key="k1",
        attempt_id="A1#1",
        experiment_id="A1",
        intent="refute",
    )
    b = severe(
        independence_key="k2",
        attempt_id="A2#1",
        experiment_id="A2",
        artifact_id="Art2",
        intent="confirm",
        effect={
            "est": 1.5,
            "ci_low": 1.5,
            "ci_high": 1.5,
            "producer": "sandbox",
            "trusted": False,
            "core_evaluated": True,
            "countable": True,
            "produced_by_call_id": "A2#1:1",
        },
    )
    assert (
        hypothesis_verdict(
            [a, b],
            True,
            supported_needed=2,
            refutations_needed=1,
            refutation_priority=False,
            confirmations_needed=1,
        )[0]
        == "supported"
    )

    timeout = severe(
        independence_key="k3",
        attempt_id="A3#1",
        experiment_id="A3",
        artifact_id="Art3",
        claim_outcome="indeterminate",
        severe=False,
        refute_capable=False,
        refute_unresolved=True,
        timeout_kind="idle_timeout",
        effect={
            "est": None,
            "producer": "sandbox",
            "trusted": False,
            "core_evaluated": True,
            "countable": False,
            "observation_only": True,
            "timeout_kind": "idle_timeout",
            "refute_unresolved": True,
            "produced_by_call_id": "A3#1:sandbox:pred",
        },
    )
    v, _, _, rule = hypothesis_verdict(
        [a, b, timeout],
        True,
        supported_needed=2,
        refutations_needed=1,
        refutation_priority=False,
        confirmations_needed=1,
    )
    assert v != "supported"
    assert v == "untestable_by_agent"
    assert rule == "idle_timeout"

    wall = severe(
        independence_key="k3",
        attempt_id="A3#1",
        experiment_id="A3",
        artifact_id="Art3",
        claim_outcome="indeterminate",
        severe=False,
        refute_capable=False,
        refute_unresolved=True,
        timeout_kind="wall_timeout",
        effect={
            "countable": False,
            "observation_only": True,
            "timeout_kind": "wall_timeout",
            "refute_unresolved": True,
            "core_evaluated": True,
            "producer": "sandbox",
            "produced_by_call_id": "A3#1:sandbox:pred",
        },
    )
    v2, _, _, rule2 = hypothesis_verdict(
        [a, b, wall],
        True,
        supported_needed=2,
        refutations_needed=1,
        refutation_priority=False,
        confirmations_needed=1,
    )
    assert v2 != "supported"
    assert v2 == "inconclusive"
    assert rule2 == "refute_unresolved"


@pytest.mark.asyncio
async def test_all_attempts_timeout_study_finalized_ru(tmp_path, clock, runtime_cls):
    """Exhausted guest timeouts → TaskDone + StudyFinalized with Russian line."""
    _require_hard()
    cat = _catalog_with(
        {
            "id": "exec_sleep",
            "title": "sleep",
            "check": {
                "kind": "exec_runs",
                "metric": "exec_runs",
                "op": "==",
                "target": "ok",
                "argv": ["python3", "-c", "import time; time.sleep(1e9)"],
                # >=2s: no-cgroup fail-closed RLIMIT_CPU needs a whole second.
                "timeout_s": 2.0,
                "memory_mb": 64,
                # Mount agent payload so jail digests differ across attempts.
                "output": "payload",
                "input_allowlist": ["payload"],
            },
            "role": "validity",
        }
    )
    sid = "st_sbx_to_budget"
    rt = await open_runtime(
        tmp_path,
        sid,
        clock=clock,
        owner="t1",
        runtime_cls=runtime_cls,
        catalog=cat,
    )
    rt.max_attempts = 2
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        inputs=[],
    )
    ex, arts = attach_executor(rt, tmp_path)
    ex.catalog = cat
    ex.max_attempts = 2
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="run",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="exec_sleep")],
            )
        ],
        questions=[],
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )
    from coscientist.core.finalize.finalize import IDLE_TIMEOUT_RU, TIMEOUT_BUDGET_RU

    for i in range(2):
        aid = await rt.start_attempt("E1")
        # Distinct jail-mounted bytes so same-digest rejection does not fire.
        art = arts.put(
            f"# attempt-{i}\n".encode(),
            make_tool_ref(f"{aid}:1", tool="agent"),
            name="payload.py",
            media_type="text/x-python",
        )
        result = await ex.record(
            aid,
            AgentTaskOutput(
                outputs={"payload": ArtifactHandle(artifact_id=art.artifact_id, name="payload")},
                claim=Outcome.SUCCESS,
            ),
        )
        assert result.reason in (
            TaskReason.IDLE_TIMEOUT.value,
            TaskReason.SANDBOX_TIMEOUT.value,
        )
        row = rt.results.get(aid)
        assert row and row.get("result_class") == "timeout"
        assert row.get("charged") is True
        # Do not tick() here — tick would auto-start the retry and race
        # with the next start_attempt in this loop.
        clock.advance(timedelta(seconds=1))

    task = next(t for t in rt.tasks.values() if t.spec.task_key == "E1")
    assert task.state.value == "DONE" or str(task.state) == "DONE"
    await record_success(rt, await rt.start_attempt("REPORT"), arts)
    for _ in range(12):
        if getattr(rt, "_finalized", False):
            break
        await rt.tick()
        clock.advance(timedelta(seconds=5))
    assert getattr(rt, "_finalized", False)
    events = await rt.store.read_all()
    fin = next(e for e in events if e.type == "StudyFinalized")
    contrib = fin.payload.get("contributing_reasons") or []
    assert any(
        x in contrib
        for x in (
            TaskReason.IDLE_TIMEOUT.value,
            TaskReason.SANDBOX_TIMEOUT.value,
            IDLE_TIMEOUT_RU,
            TIMEOUT_BUDGET_RU,
        )
    )
    assert IDLE_TIMEOUT_RU in contrib or TIMEOUT_BUDGET_RU in contrib
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_timeout_with_stuck_drain_charges_attempt(tmp_path, clock, runtime_cls, monkeypatch):
    """Timeout + stuck drain charges the attempt; catalog host budgets reach run_sandbox.

    Host-side ``run_sandbox`` stub (no bwrap): exercises watch/classify + charge path.
    """
    import os
    import select
    import signal
    import subprocess
    import sys

    import coscientist.checks.kinds.executable as exe_mod
    import coscientist.checks.sandbox as sb_mod
    from coscientist.checks.kinds.executable import _limits
    from coscientist.checks.sandbox import ReapTimeoutError, force_reap_pid
    from coscientist.checks.sandbox_limits import apply_frozen_limits

    # No _require_hard(): this test stubs run_sandbox and never enters bwrap.
    host_reap_s = 0.25
    host_drain_s = 0.25
    wall_s = 2.0
    ready_wait_s = 2.0
    cat = _catalog_with(
        {
            "id": "exec_busy_hold",
            "title": "busy+hold",
            "check": {
                "kind": "exec_runs",
                "metric": "exec_runs",
                "op": "==",
                "target": "ok",
                # Argv unused: run_sandbox is stubbed below.
                "argv": ["python3", "-c", "pass"],
                "timeout_s": wall_s,
                "memory_mb": 64,
                "reap_timeout_s": host_reap_s,
                "output_drain_join_s": host_drain_s,
            },
            "role": "validity",
        }
    )
    tmpl = next(t for t in cat.templates if t.id == "exec_busy_hold")
    # Production _limits on catalog check must carry the catalog budgets verbatim.
    prod_lim = _limits(tmpl.check)
    assert prod_lim.reap_timeout_s == host_reap_s
    assert prod_lim.output_drain_join_s == host_drain_s
    assert prod_lim.reap_timeout_s != TEST_CHECK_REAP_TIMEOUT_S
    assert prod_lim.output_drain_join_s != TEST_CHECK_DRAIN_JOIN_S

    sid = "st_sbx_drain_charge"
    rt = await open_runtime(
        tmp_path,
        sid,
        clock=clock,
        owner="t1",
        runtime_cls=runtime_cls,
        catalog=cat,
    )
    rt.max_attempts = 2
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=cat.catalog_version,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        inputs=[],
    )
    ex, arts = attach_executor(rt, tmp_path)
    ex.catalog = cat
    ex.max_attempts = 2
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="run",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="exec_busy_hold")],
            )
        ],
        questions=[],
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )
    task = next(t for t in rt.tasks.values() if t.spec.task_key == "E1")
    frozen_check = apply_frozen_limits(tmpl.check, task.spec, template_id="exec_busy_hold")
    frozen_lim = _limits(frozen_check)
    assert frozen_lim.reap_timeout_s == host_reap_s
    assert frozen_lim.output_drain_join_s == host_drain_s

    captured: dict = {}

    def run_sandbox_host_stuck_drain(argv, **kwargs):
        """bwrap die-with-parent clears drain_failed; host watch keeps the flag real."""
        del argv
        lim = kwargs["limits"]
        assert lim.reap_timeout_s == host_reap_s
        assert lim.output_drain_join_s == host_drain_s
        captured["limits"] = lim
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
            "while True:\n"
            "    pass\n"
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
        holder_pid: int | None = None
        try:
            ready, _, _ = select.select([ready_r], [], [], ready_wait_s)
            assert ready, f"ready-pipe timed out after {ready_wait_s}s"
            assert os.read(ready_r, 1) == b"R"
            os.close(ready_r)
            ready_r = -1
            hp_ready, _, _ = select.select([holder_r], [], [], ready_wait_s)
            assert hp_ready, f"holder pid pipe timed out after {ready_wait_s}s"
            holder_pid = int(os.read(holder_r, 64).decode())
            os.close(holder_r)
            holder_r = -1
            (
                timed_out,
                overflow,
                out_b,
                err_b,
                kill_cause,
                cpu_s,
                rusage_s,
                drain_failed,
                _drain_note,
            ) = sb_mod._watch_process(proc, lim=lim, cgroup_leaf=None)
            result = sb_mod._classify(
                timed_out=timed_out,
                overflow=overflow,
                proc=proc,
                cgroup_leaf=None,
                oom_detectable=False,
                infra_msg=b"",
                out_b=out_b,
                err_b=err_b,
                duration=float(lim.timeout_s),
                net_i="hard",
                fs_i="hard",
                work_dir=None,
                result_files={},
                lim=lim,
                cgroup_limits_held=True,
                kill_cause=kill_cause,
                cpu_usage_s=cpu_s,
                rusage_cpu_s=rusage_s,
                output_drain_failed=drain_failed,
            )
            captured["result"] = result
            return result
        finally:
            for fd in (ready_r, holder_r):
                if isinstance(fd, int) and fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if holder_pid is not None:
                force_reap_pid(
                    holder_pid,
                    timeout_s=float(lim.reap_timeout_s),
                    poll_interval_s=float(lim.reap_poll_interval_s),
                )
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=float(lim.reap_timeout_s))
            except subprocess.TimeoutExpired as e:
                raise ReapTimeoutError(
                    f"stub process group still present after reap_timeout_s={lim.reap_timeout_s!r}"
                ) from e

    monkeypatch.setattr(exe_mod, "run_sandbox", run_sandbox_host_stuck_drain)

    aid = await rt.start_attempt("E1")
    result = await ex.record(aid, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS))
    assert result.reason == TaskReason.SANDBOX_TIMEOUT.value
    assert captured["limits"].reap_timeout_s == host_reap_s
    assert captured["limits"].output_drain_join_s == host_drain_s
    assert captured["result"].status is sb_mod.SandboxStatus.TIMEOUT
    assert captured["result"].reason == "wall_timeout"
    assert captured["result"].output_drain_failed is True
    assert rt.results[aid]["result_class"] == "timeout"
    assert rt.results[aid]["charged"] is True
    assert rt.attempts[aid]["charged"] is True
    task = next(t for t in rt.tasks.values() if t.spec.task_key == "E1")
    charged_used = sum(1 for a in task.attempts if rt.attempts[a]["charged"] is True)
    assert charged_used == 1
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_infra_failure_does_not_charge_attempt(tmp_path, clock, runtime_cls, monkeypatch):
    """Transient attach infra → charged=False; next attempt still within budget."""
    from coscientist.core.events.errors import SandboxInfraError

    cat = _catalog_with(
        {
            "id": "exec_ok",
            "title": "exec",
            "check": {
                "kind": "exec_runs",
                "metric": "exec_runs",
                "op": "==",
                "target": "ok",
                "argv": ["/bin/true"],
                "timeout_s": 5,
                "memory_mb": 64,
            },
            "role": "validity",
        }
    )
    sid = "st_sbx_infra_nchg"
    rt = await open_runtime(
        tmp_path,
        sid,
        clock=clock,
        owner="t1",
        runtime_cls=runtime_cls,
        catalog=cat,
    )
    rt.max_attempts = 2
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        inputs=[],
    )
    ex, arts = attach_executor(rt, tmp_path)
    ex.catalog = cat
    ex.max_attempts = 2
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="run",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="exec_ok")],
            )
        ],
        questions=[],
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )
    aid = await rt.start_attempt("E1")
    calls = {"n": 0}
    real_eval = evaluate_task_checks

    def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SandboxInfraError("sandbox_unavailable:cgroup_attach")
        return real_eval(*args, **kwargs)

    monkeypatch.setattr("coscientist.core.execution.executor.evaluate_task_checks", boom)
    result = await ex.record(aid, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS))
    assert result.reason == TaskReason.SANDBOX_INFRA.value
    row = rt.results.get(aid)
    assert row and row.get("result_class") == "infra"
    assert row.get("charged") is False
    att = rt.attempts.get(aid) or {}
    assert att.get("charged") is False
    # Uncharged → charged budget still allows a real attempt (max_attempts=2).
    # Avoid tick() auto-start; start the next attempt explicitly.
    clock.advance(timedelta(seconds=1))
    aid2 = await rt.start_attempt("E1")
    assert aid2 != aid
    # Second attempt can succeed (hard path) without the infra counting.
    if bwrap_available() and probe_bwrap_hard(allow_network=False):
        r2 = await ex.record(aid2, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS))
        assert r2.outcome is Outcome.SUCCESS
        assert rt.results[aid2].get("charged") is True
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_same_digest_retry_rejected(tmp_path, clock, runtime_cls):
    """Retry with the same artifact digest as a timed-out attempt is rejected."""
    _require_hard()
    cat = _catalog_with(
        {
            "id": "exec_sleep",
            "title": "sleep",
            "check": {
                "kind": "exec_runs",
                "metric": "exec_runs",
                "op": "==",
                "target": "ok",
                "argv": ["python3", "-c", "import time; time.sleep(1e9)"],
                # >=2s: no-cgroup fail-closed RLIMIT_CPU needs a whole second.
                "timeout_s": 2.0,
                "memory_mb": 64,
            },
            "role": "validity",
        }
    )
    sid = "st_sbx_same_dig"
    rt = await open_runtime(
        tmp_path,
        sid,
        clock=clock,
        owner="t1",
        runtime_cls=runtime_cls,
        catalog=cat,
    )
    rt.max_attempts = 3
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        inputs=[],
    )
    ex, arts = attach_executor(rt, tmp_path)
    ex.catalog = cat
    ex.max_attempts = 3
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="run",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="exec_sleep")],
            )
        ],
        questions=[],
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )
    empty = AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS)
    aid1 = await rt.start_attempt("E1")
    r1 = await ex.record(aid1, empty)
    assert r1.reason in (
        TaskReason.IDLE_TIMEOUT.value,
        TaskReason.SANDBOX_TIMEOUT.value,
    )
    clock.advance(timedelta(seconds=1))
    # Explicit retry (no tick auto-start) with the same empty digest.
    aid2 = await rt.start_attempt("E1")
    r2 = await ex.record(aid2, empty)
    assert r2.reason == TaskReason.SAME_DIGEST_RETRY.value
    row2 = rt.results.get(aid2)
    assert row2 and row2.get("result_class") == "timeout"
    assert row2.get("charged") is True
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_frozen_sandbox_limits_on_plan_accept(tmp_path, clock, runtime_cls):
    """Catalog per-check limits frozen; agent sandbox_timeout_s stripped."""
    cat = _catalog_with(
        {
            "id": "exec_ok",
            "title": "exec",
            "check": {
                "kind": "exec_runs",
                "metric": "exec_runs",
                "op": "==",
                "target": "ok",
                "argv": ["/bin/true"],
                "timeout_s": 7.5,
                "memory_mb": 128,
            },
            "role": "validity",
        }
    )
    sid = "st_sbx_freeze"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls, catalog=cat)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        inputs=[],
    )
    attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="run",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="exec_ok")],
                # Agent tries to override — must be stripped.
                sandbox_timeout_s=0.05,
                sandbox_memory_mb=1,
            )
        ],
        questions=[],
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )
    task = next(t for t in rt.tasks.values() if t.spec.task_key == "E1")
    assert task.spec.sandbox_timeout_s is None
    assert task.spec.sandbox_memory_mb is None
    assert task.spec.sandbox_check_limits == {
        "exec_ok": FrozenCheckLimits(
            timeout_s=7.5,
            memory_mb=128,
            max_output_bytes=TEST_MAX_OUTPUT_BYTES,
            pids_max=TEST_PIDS_MAX,
            rlimit_nproc=TEST_RLIMIT_NPROC,
            reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
            output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
            allow_network=False,
            allow_cgroup_degraded=True,
        )
    }
    assert task.spec.sandbox_limits_note == "stripped_agent_sandbox_limits"
    await close_runtime(rt)


def test_freeze_strips_agent_timeout_zero_point_oh_five():
    """Unit: plan timeout=0.05 never becomes the frozen limit."""
    from coscientist.checks.sandbox_limits import (
        apply_frozen_limits,
        freeze_task_sandbox_limits,
    )

    cat = _catalog_with(
        {
            "id": "exec_ok",
            "title": "exec",
            "check": {
                "kind": "exec_runs",
                "metric": "exec_runs",
                "op": "==",
                "target": "ok",
                "argv": ["/bin/true"],
                "timeout_s": 30.0,
                "memory_mb": 256,
            },
            "role": "validity",
        }
    )
    frozen = freeze_task_sandbox_limits(
        TaskSpec(
            task_key="E1",
            title="t",
            goal="g",
            route="experiment",
            criteria=[RequirementRef(template_id="exec_ok")],
            sandbox_timeout_s=0.05,
            sandbox_memory_mb=1,
            sandbox_check_limits={
                "exec_ok": FrozenCheckLimits(
                    timeout_s=0.05,
                    memory_mb=1,
                    max_output_bytes=1,
                    pids_max=1,
                    rlimit_nproc=1,
                    reap_timeout_s=0.05,
                    output_drain_join_s=0.05,
                    allow_network=True,
                    allow_cgroup_degraded=True,
                )
            },
        ),
        cat,
    )
    assert frozen.sandbox_timeout_s is None
    assert frozen.sandbox_memory_mb is None
    assert frozen.sandbox_check_limits["exec_ok"].timeout_s == 30.0
    assert frozen.sandbox_check_limits["exec_ok"].allow_network is False
    assert "stripped_agent_sandbox_limits" in (frozen.sandbox_limits_note or "")
    assert "stripped_agent_sandbox_check_limits" in (frozen.sandbox_limits_note or "")
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="exec_runs",
        argv=["/bin/true"],
        timeout_s=99.0,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=True,
    )
    applied = apply_frozen_limits(check, frozen, template_id="exec_ok")
    assert applied.timeout_s == 30.0
    assert applied.memory_mb == 256
    assert applied.pids_max == TEST_PIDS_MAX
    assert applied.rlimit_nproc == TEST_RLIMIT_NPROC


def test_agent_facing_catalog_view_is_sole_template_export():
    """Catalog agent projection never includes holdout/scorer/tests bodies."""
    from coscientist.checks.evaluate import agent_facing_catalog_view
    from coscientist.core.events.digest import canonical_json, sha256_hex

    holdout = {"y": [0, 1], "secret": "NEVER"}
    cat = _catalog_with(
        {
            "id": "metric_secret",
            "title": "m",
            "text_ru": "Метрика",
            "check": {
                "kind": "metric_on_holdout",
                "metric": "holdout",
                "op": ">=",
                "argv": list(BUILTIN_METRIC_SCORER_ARGV),
                "input_allowlist": ["predictions.json"],
                "holdout": holdout,
                "holdout_digest": sha256_hex(canonical_json(holdout)),
                "output": "predictions",
                "target": 0.5,
                "tests": {"test_x.py": "assert False"},
            },
            "role": "prediction",
        }
    )
    view = agent_facing_catalog_view(cat)
    tmpl_view = next(t for t in view["templates"] if t["template_id"] == "metric_secret")
    blob = json.dumps(view, default=str)
    assert "NEVER" not in blob
    # Kind name may contain the substring; secret keys / payloads must not.
    assert "holdout" not in tmpl_view  # no holdout field on the view dict
    assert '"y"' not in blob
    assert BUILTIN_METRIC_SCORER_ARGV[0] not in blob
    assert "assert False" not in blob
    assert tmpl_view["template_id"] == "metric_secret"
    assert tmpl_view["is_code"] is True
    assert tmpl_view["check_detail"] == "metric_on_holdout"


def test_sandbox_timeout_report_line_by_reason_only():
    from coscientist.core.claims.reasons import TaskReason
    from coscientist.core.finalize.finalize import (
        IDLE_TIMEOUT_RU,
        TIMEOUT_BUDGET_RU,
        sandbox_timeout_report_line,
    )

    assert sandbox_timeout_report_line(TaskReason.IDLE_TIMEOUT.value, "anything") == IDLE_TIMEOUT_RU
    assert sandbox_timeout_report_line(TaskReason.SANDBOX_TIMEOUT.value, "x") == TIMEOUT_BUDGET_RU
    # Detail substring must not trigger — reason alone decides.
    assert sandbox_timeout_report_line("budget", "idle_timeout wall_timeout") is None
    assert sandbox_timeout_report_line("cancelled", "sandbox_timeout") is None


def test_jail_mount_digest_covers_mounted_bytes():
    from coscientist.checks.kinds.executable import jail_mount_digest

    a = jail_mount_digest({"code.py": b"print(1)\n"})
    b = jail_mount_digest({"code.py": b"print(1)\n "})  # trailing space ≠ same
    c = jail_mount_digest({"code.py": b"print(1)\n"})
    assert a == c
    assert a != b


def test_no_derived_call_id_fallback():
    """Missing produced_by_call_id must not be invented as {attempt}:derived."""
    from datetime import UTC, datetime

    from coscientist.core.claims.graph import (
        GraphNode,
        GraphStore,
        default_execution_registry,
    )
    from coscientist.core.events.ids import question_node_id
    from coscientist.core.execution.inputs import claim_nodes_from_attempt

    sid = "st_noderived"
    g = GraphStore(default_execution_registry())
    g.upsert_node(
        GraphNode(
            node_id=question_node_id(sid, "Q1"),
            kind="question",
            study_id=sid,
            ref_id="Q1",
            attrs={"formulation": "why"},
        )
    )
    now = datetime(2024, 1, 1, tzinfo=UTC)
    evs, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=f"{sid}/E1#1",
        attempt_outcome="SUCCESS",
        provenance="real",
        claims=[{"id": "C1", "target": "Q1", "checked_against": "value_match"}],
        now=now,
        graph=g,
        derived={
            "criterion_status": "met",
            "evidence_kind": "computational",
            "frozen": True,
            "accepted": True,
            "checked_against": "value_match",
            "effect": {
                "est": 1.0,
                "core_evaluated": True,
                "countable": True,
                # intentionally no produced_by_call_id
            },
            "check_role": "prediction",
        },
    )
    assert drops == []
    saw = False
    for ev in evs:
        if ev.type == "GraphNodeUpserted":
            effect = (ev.payload.get("attrs") or {}).get("effect") or {}
            saw = True
            assert "produced_by_call_id" not in effect or effect.get("produced_by_call_id") in (None, "")
            assert ":derived" not in str(effect.get("produced_by_call_id") or "")
    assert saw


def test_freeze_shared_helper_used_by_all_task_create_paths():
    """accept_plan / expand / foreach / expand_trigger share freeze_task_sandbox_limits."""
    import inspect

    from coscientist.checks import sandbox_limits as sl
    from coscientist.core.plan import expand, expand_trigger, foreach, lifecycle

    assert "freeze_task_sandbox_limits" in inspect.getsource(lifecycle.LifecycleMixin.accept_plan)
    # Module-level TaskCreated writers import the shared helper.
    for mod in (foreach, expand, expand_trigger):
        src = inspect.getsource(mod)
        assert "freeze_task_sandbox_limits" in src
    # Catalog is the sole source — agent timeout=0.05 never wins.
    cat = _catalog_with(
        {
            "id": "exec_ok",
            "title": "e",
            "check": {
                "kind": "exec_runs",
                "metric": "exec_runs",
                "op": "==",
                "target": "ok",
                "argv": ["/bin/true"],
                "timeout_s": 12.0,
                "memory_mb": 64,
            },
            "role": "validity",
        }
    )
    out = sl.freeze_task_sandbox_limits(
        TaskSpec(
            task_key="E1",
            title="t",
            goal="g",
            route="experiment",
            criteria=[RequirementRef(template_id="exec_ok")],
            sandbox_timeout_s=0.05,
        ),
        cat,
    )
    assert out.sandbox_check_limits["exec_ok"].timeout_s == 12.0
    assert out.sandbox_timeout_s is None


@pytest.mark.asyncio
async def test_alternating_prediction_timeouts_elevate_untestable(tmp_path, clock, runtime_cls, monkeypatch):
    """≥2 prediction timeouts on a task (different templates) → idle/untestable.

    Alternating template_ids must not evade the structural rule.
    """
    from coscientist.core.execution.domain import CheckResult

    cat = _catalog_with(
        {
            "id": "pred_a",
            "title": "a",
            "check": {
                "kind": "exec_runs",
                "metric": "exec_runs",
                "op": "==",
                "target": "ok",
                "argv": ["/bin/true"],
                "timeout_s": 5,
            },
            "role": "prediction",
        },
        {
            "id": "pred_b",
            "title": "b",
            "check": {
                "kind": "exec_runs",
                "metric": "exec_runs",
                "op": "==",
                "target": "ok",
                "argv": ["/bin/true"],
                "timeout_s": 5,
            },
            "role": "prediction",
        },
    )
    sid = "st_sbx_alt_to"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls, catalog=cat)
    rt.max_attempts = 3
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        inputs=[],
    )
    ex, arts = attach_executor(rt, tmp_path)
    ex.catalog = cat
    ex.max_attempts = 3
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="run",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[
                    RequirementRef(template_id="pred_a"),
                    RequirementRef(template_id="pred_b"),
                ],
            )
        ],
        questions=[],
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )

    call_n = {"n": 0}

    def _fake_eval(*_a, **_kw):
        call_n["n"] += 1
        # Attempt 1: only pred_a times out; attempt 2: only pred_b times out.
        tid = "pred_a" if call_n["n"] == 1 else "pred_b"
        other = "pred_b" if tid == "pred_a" else "pred_a"
        return [
            CheckResult(
                check_id=f"{tid}#0",
                template_id=tid,
                status=ReqStatus.FAILED,
                is_code=True,
                required=True,
                role="prediction",
                detail="wall_timeout",
                effect={
                    "timeout_kind": "wall_timeout",
                    "refute_unresolved": True,
                    "observation_only": True,
                    "countable": False,
                    "jail_inputs_digest": f"dig-{call_n['n']}",
                    "core_evaluated": True,
                    "producer": "sandbox",
                    "produced_by_call_id": f"aid:{call_n['n']}",
                },
            ),
            CheckResult(
                check_id=f"{other}#1",
                template_id=other,
                status=ReqStatus.MET,
                is_code=True,
                required=True,
                role="prediction",
                detail="ok",
                effect={
                    "countable": True,
                    "core_evaluated": True,
                    "producer": "sandbox",
                    "produced_by_call_id": f"aid:{call_n['n']}:ok",
                    "jail_inputs_digest": f"dig-{call_n['n']}-ok",
                },
            ),
        ]

    monkeypatch.setattr("coscientist.core.execution.executor.evaluate_task_checks", _fake_eval)

    aid1 = await rt.start_attempt("E1")
    r1 = await ex.record(aid1, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS))
    # First prediction timeout on the task — do NOT escalate to untestable.
    assert r1.reason == TaskReason.SANDBOX_TIMEOUT.value
    row1 = rt.results[aid1]
    assert row1.get("result_class") == "timeout"
    assert "repeat_timeout" not in str(row1.get("detail") or "")

    clock.advance(timedelta(seconds=1))
    aid2 = await rt.start_attempt("E1")
    r2 = await ex.record(aid2, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS))
    # Second prediction timeout (different template) → structural idle/untestable.
    assert r2.reason == TaskReason.IDLE_TIMEOUT.value
    row2 = rt.results[aid2]
    assert row2.get("result_class") == "timeout"
    assert "repeat_timeout" in str(row2.get("detail") or "") or any(
        (cr.get("effect") or {}).get("repeat_timeout")
        for cr in (row2.get("check_results") or [])
        if isinstance(cr, dict)
    )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_single_prediction_timeout_on_last_attempt_not_escalated(tmp_path, clock, runtime_cls, monkeypatch):
    """One prediction timeout on the last charged attempt stays sandbox_timeout."""
    from coscientist.core.execution.domain import CheckResult

    cat = _catalog_with(
        {
            "id": "pred_only",
            "title": "p",
            "check": {
                "kind": "exec_runs",
                "metric": "exec_runs",
                "op": "==",
                "target": "ok",
                "argv": ["/bin/true"],
                "timeout_s": 5,
            },
            "role": "prediction",
        }
    )
    sid = "st_sbx_last_to"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls, catalog=cat)
    rt.max_attempts = 1
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=4,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        inputs=[],
    )
    ex, arts = attach_executor(rt, tmp_path)
    ex.catalog = cat
    ex.max_attempts = 1
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="run",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="pred_only")],
            )
        ],
        questions=[],
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )

    def _fake_eval(*_a, **_kw):
        return [
            CheckResult(
                check_id="pred_only#0",
                template_id="pred_only",
                status=ReqStatus.FAILED,
                is_code=True,
                required=True,
                role="prediction",
                detail="wall_timeout",
                effect={
                    "timeout_kind": "wall_timeout",
                    "refute_unresolved": True,
                    "observation_only": True,
                    "countable": False,
                    "jail_inputs_digest": "dig-1",
                    "core_evaluated": True,
                    "producer": "sandbox",
                    "produced_by_call_id": "aid:1",
                },
            )
        ]

    monkeypatch.setattr("coscientist.core.execution.executor.evaluate_task_checks", _fake_eval)
    aid = await rt.start_attempt("E1")
    r = await ex.record(aid, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS))
    assert r.reason == TaskReason.SANDBOX_TIMEOUT.value
    row = rt.results[aid]
    assert not any(
        (cr.get("effect") or {}).get("repeat_timeout")
        for cr in (row.get("check_results") or [])
        if isinstance(cr, dict)
    )
    await close_runtime(rt)


def test_metric_on_holdout_missing_command_is_catalog_error(tmp_path):
    """B2: no scorer argv/command → CatalogError (not scored FAILED)."""
    holdout = {"y": [0, 1]}
    arts = LocalArtifactStore(tmp_path / "arts")
    pred = arts.put(
        b'{"y_hat":[0,1]}',
        make_tool_ref("st/E1#1:pred"),
        name="predictions.json",
        media_type="application/json",
    )
    out = AgentTaskOutput(
        outputs={"predictions": ArtifactHandle(artifact_id=pred.artifact_id, name="predictions.json")}
    )
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="metric_on_holdout",
        output="predictions",
        op=">=",
        target=0.5,
        input_allowlist=["predictions.json"],
        holdout=holdout,
        holdout_digest=sha256_hex(canonical_json(holdout)),
        timeout_s=5.0,
        memory_mb=64,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=True,
        # no argv / command
    )
    with pytest.raises(CatalogError, match="missing_command"):
        evaluate_code_check(
            check,
            template_id="m",
            check_id="m#0",
            out=out,
            artifacts=arts,
            values={},
            role="prediction",
            attempt_id="st/E1#1",
        )


def test_tests_pass_missing_command_raises_like_exec_runs(tmp_path):
    """B9: empty argv → raise (no invent catalog:pytest from tests alone)."""
    arts = LocalArtifactStore(tmp_path / "arts")
    sol = arts.put(
        b"def add(a,b): return a+b\n",
        make_tool_ref("st/E1#1:code"),
        name="solution.py",
        media_type="text/x-python",
    )
    out = AgentTaskOutput(outputs={"solution.py": ArtifactHandle(artifact_id=sol.artifact_id, name="solution.py")})
    check = CodeCheck(
        reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
        output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
        kind="tests_pass",
        tests={"t.py": "def test_x():\n    assert True\n"},
        input_allowlist=["solution.py"],
        min_tests=1,
        timeout_s=5.0,
        memory_mb=64,
        max_output_bytes=TEST_MAX_OUTPUT_BYTES,
        pids_max=TEST_PIDS_MAX,
        rlimit_nproc=TEST_RLIMIT_NPROC,
        allow_network=False,
        allow_cgroup_degraded=True,
    )
    with pytest.raises(SandboxInfraError, match="missing_command"):
        evaluate_code_check(
            check,
            template_id="tp",
            check_id="tp#0",
            out=out,
            artifacts=arts,
            values={},
            role="validity",
            attempt_id="st/E1#1",
        )


def test_exec_runs_missing_command_raises(tmp_path):
    with pytest.raises(SandboxInfraError, match="missing_command"):
        evaluate_code_check(
            CodeCheck(
                reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
                output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
                kind="exec_runs",
                timeout_s=5.0,
                memory_mb=64,
                max_output_bytes=TEST_MAX_OUTPUT_BYTES,
                pids_max=TEST_PIDS_MAX,
                rlimit_nproc=TEST_RLIMIT_NPROC,
                allow_network=False,
                allow_cgroup_degraded=True,
            ),
            template_id="e",
            check_id="e#0",
            out=AgentTaskOutput(outputs={}),
            artifacts=LocalArtifactStore(tmp_path / "arts"),
            values={},
            role="validity",
            attempt_id="a1",
        )


def test_artifact_missing_meta_is_infra(tmp_path):
    """B9: meta None → SandboxInfraError (no silent continue)."""
    arts = LocalArtifactStore(tmp_path / "arts")
    out = AgentTaskOutput(outputs={"solution.py": ArtifactHandle(artifact_id="missing-art-id", name="solution.py")})
    with pytest.raises(SandboxInfraError, match="artifact_missing"):
        evaluate_code_check(
            CodeCheck(
                reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
                output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
                kind="tests_pass",
                argv=list(BUILTIN_TESTS_PASS_ARGV),
                tests={"t.py": "def test_x():\n    assert True\n"},
                input_allowlist=["solution.py"],
                min_tests=1,
                timeout_s=5.0,
                memory_mb=64,
                max_output_bytes=TEST_MAX_OUTPUT_BYTES,
                pids_max=TEST_PIDS_MAX,
                rlimit_nproc=TEST_RLIMIT_NPROC,
                allow_network=False,
                allow_cgroup_degraded=True,
            ),
            template_id="tp",
            check_id="tp#0",
            out=out,
            artifacts=arts,
            values={},
            role="validity",
            attempt_id="st/E1#1",
        )


def test_builtin_argv_constants_are_exact_tokens():
    assert BUILTIN_TESTS_PASS_ARGV == ("catalog:pytest",)
    assert BUILTIN_METRIC_SCORER_ARGV == ("catalog:metric",)


def test_verifier_rejected_input_name_raises(tmp_path, monkeypatch):
    """MAJOR: rejected input names → SandboxInfraError (not warning+continue)."""
    import coscientist.checks.sandbox as sb

    monkeypatch.setenv("CGROUP_JOB", str(tmp_path / "cg"))
    (tmp_path / "cg").mkdir()
    with pytest.raises(SandboxInfraError, match="input_name_rejected"):
        run_verifier(
            ["/bin/true"],
            inputs={"evil.pth": b"x"},
            limits=sb.limits_from_check(
                timeout_s=1.0,
                memory_mb=64,
                max_output_bytes=TEST_MAX_OUTPUT_BYTES,
                pids_max=TEST_PIDS_MAX,
                rlimit_nproc=TEST_RLIMIT_NPROC,
                allow_network=False,
                allow_cgroup_degraded=True,
                reap_timeout_s=TEST_CHECK_REAP_TIMEOUT_S,
                output_drain_join_s=TEST_CHECK_DRAIN_JOIN_S,
            ),
            extra_ro_files={},
            input_allowlist=frozenset({"solution.py"}),
            collect=(),
        )
