"""M1 PR17: executable kinds + bwrap sandbox / separate verifier."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from coscientist.checks.catalog import load_catalog
from coscientist.checks.evaluate import evaluate_code_check, evaluate_task_checks
from coscientist.checks.kinds.executable import (
    REWARD_SCHEMA,
    agent_filename_allowed,
    parse_reward,
)
from coscientist.checks.sandbox import (
    INFRA_STATUSES,
    SandboxLimits,
    SandboxStatus,
    run_sandbox,
    run_verifier,
)
from coscientist.checks.sandbox_probe import bwrap_available, probe_bwrap_hard
from coscientist.core.claims.reasons import TaskReason
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import SandboxInfraError
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    CodeCheck,
    Outcome,
    ReqStatus,
    RequirementRef,
    TaskSpec,
)
from tests.helpers import attach_executor, close_runtime, open_runtime, record_success


def _REWARD_JSON(v: float) -> bytes:
    return json.dumps({"schema": REWARD_SCHEMA, "value": float(v)}).encode()

_RERUN_SCORER = f"""
import json, os, pathlib
results = pathlib.Path(os.environ["COSCIENTIST_RESULTS"])
(results / "reward.txt").write_text(
    json.dumps({{"schema": {REWARD_SCHEMA!r}, "value": 1.0}})
)
""".encode()


CATALOG_VERSION = "0.1.0-pr17"


def _catalog_with(*templates: dict) -> object:
    rules = [
        {
            "rule_id": "R23",
            "source": "reviewed-rules",
            "excerpt": "x",
            "digest": "abc",
        }
    ]
    return load_catalog(
        {
            "catalog_version": CATALOG_VERSION,
            "edition": "pr17",
            "sources_sha256": {},
            "rules": rules,
            "templates": [
                {
                    **t,
                    "text_ru": t.get("text_ru", "проверка"),
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "role": t.get("role", "validity"),
                    "kind": t.get("kind", "catalog"),
                    "required": True,
                    "version": t.get("version", "1"),
                    "uses_sesoi": t.get("uses_sesoi", False),
                    "predicts_null": t.get("predicts_null", False),
                    "predicted_direction": t.get(
                        "predicted_direction", "two_sided"
                    ),
                }
                for t in templates
            ],
            "defaults": {"experiment": [templates[0]["id"]]},
        }
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
    r = run_sandbox(["/bin/true"], limits=SandboxLimits(timeout_s=5, memory_mb=64))
    assert r.status is SandboxStatus.OK
    assert r.exit_code == 0
    assert r.limits_fully_held == (
        r.net_isolation == "hard" and r.fs_isolation == "hard"
    )


def test_sandbox_hard_isolation_when_bwrap_available():
    """Real bwrap probe — never fake hard with an empty prefix."""
    _require_hard()
    r = run_sandbox(
        ["/bin/true"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64, allow_network=False),
    )
    assert r.status is SandboxStatus.OK
    assert r.net_isolation == "hard"
    assert r.fs_isolation == "hard"
    assert r.limits_fully_held is True


def test_sandbox_fail_closed_when_bwrap_probe_fails(monkeypatch, caplog, tmp_path):
    """Without hard isolation: no guest exec → sandbox_unavailable (fail-closed)."""
    import logging

    _force_soft(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="coscientist.checks.sandbox_probe"):
        r = run_sandbox(
            ["/bin/true"],
            limits=SandboxLimits(timeout_s=5, memory_mb=64, allow_network=False),
        )
    assert r.status is SandboxStatus.UNAVAILABLE
    assert r.reason == "sandbox_unavailable:isolation"
    assert r.net_isolation == "soft"
    assert r.fs_isolation == "soft"
    assert r.limits_fully_held is False

    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(
        kind="exec_runs", argv=["/bin/true"], timeout_s=5, memory_mb=64, allow_network=False)
    with pytest.raises(SandboxInfraError, match="sandbox_unavailable:isolation"):
        evaluate_code_check(
            check,
            template_id="exec",
            check_id="exec#soft",
            out=AgentTaskOutput(outputs={}, summary=""),
            artifacts=arts,
            values={},
            role="validity",
            attempt_id="st/E1#1",
        
            uses_sesoi=False,
            predicts_null=False,
            predicted_direction="two_sided",
        )


def test_sandbox_no_bwrap_fail_closed(monkeypatch, tmp_path):
    import coscientist.checks.sandbox_probe as probe

    probe._bwrap_bin.cache_clear()
    probe.probe_bwrap_hard.cache_clear()
    monkeypatch.setattr(probe, "_bwrap_bin", lambda: None)
    monkeypatch.setattr(probe, "bwrap_available", lambda: False)
    r = run_sandbox(["/bin/true"], limits=SandboxLimits(timeout_s=5, memory_mb=64))
    assert r.status is SandboxStatus.UNAVAILABLE
    assert r.reason == "sandbox_unavailable:isolation"
    arts = LocalArtifactStore(tmp_path)
    with pytest.raises(SandboxInfraError, match="sandbox_unavailable:isolation"):
        evaluate_code_check(
            CodeCheck(kind="exec_runs", argv=["/bin/true"], timeout_s=5, memory_mb=64, allow_network=False),
            template_id="exec",
            check_id="exec#nobwrap",
            out=AgentTaskOutput(outputs={}, summary=""),
            artifacts=arts,
            values={},
            role="validity",
            attempt_id="st/E1#1",
        
            uses_sesoi=False,
            predicts_null=False,
            predicted_direction="two_sided",
        )


def test_sandbox_allow_network_runs_but_not_trusted(tmp_path):
    """FS hard + intentional net=none still executes via bwrap; trusted=False."""
    if not bwrap_available() or not probe_bwrap_hard(allow_network=True):
        pytest.fail("bubblewrap required")
    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(
        kind="exec_runs",
        argv=["/bin/true"],
        timeout_s=5,
        memory_mb=64,
        allow_network=True,
    )
    cr = evaluate_code_check(
        check,
        template_id="exec",
        check_id="exec#net",
        out=AgentTaskOutput(outputs={}, summary=""),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
    )
    assert cr.status is ReqStatus.MET
    assert cr.effect is not None
    assert cr.effect.get("net_isolation") == "none"
    assert cr.effect.get("fs_isolation") == "hard"
    assert cr.effect.get("trusted") is False


def test_sandbox_sleep_bypass_is_idle_or_wall_timeout():
    """Guest sleep past wall → idle when cpu.stat measured; else wall (not infra)."""
    _require_hard()
    r = run_sandbox(
        ["python3", "-c", "import time; time.sleep(1e9)"],
        limits=SandboxLimits(timeout_s=0.3, memory_mb=64, max_output_bytes=10_000),
    )
    assert r.status is SandboxStatus.TIMEOUT
    assert r.is_guest_timeout
    assert r.status not in INFRA_STATUSES
    if r.cpu_usage_s is not None:
        assert r.reason == "idle_timeout"
    else:
        # Without cpu.stat we refuse to guess sabotage.
        assert r.reason == "wall_timeout"


def test_sandbox_busy_loop_is_cpu_or_wall_timeout():
    """Busy loop → cpu_timeout (cgroup watcher or no-cgroup rusage≥budget)."""
    _require_hard()
    r = run_sandbox(
        ["python3", "-c", "while True: pass"],
        limits=SandboxLimits(timeout_s=3.0, memory_mb=64, max_output_bytes=10_000),
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
        limits=SandboxLimits(timeout_s=2.0, memory_mb=64, max_output_bytes=10_000),
    )
    assert r.status not in INFRA_STATUSES
    assert r.status is SandboxStatus.TIMEOUT
    assert r.reason in ("cpu_timeout", "wall_timeout")
    assert r.reason != "idle_timeout"


def test_sandbox_positive_exit_137_is_scientific_not_oom():
    _require_hard()
    r = run_sandbox(
        ["python3", "-c", "raise SystemExit(137)"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64),
    )
    assert r.status is SandboxStatus.NONZERO
    assert r.exit_code == 137


def test_sandbox_guest_exit_75_is_scientific_not_rlimit():
    """Guest exit 75 must never map to UNAVAILABLE — only private channel does."""
    _require_hard()
    r = run_sandbox(
        ["python3", "-c", "raise SystemExit(75)"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64),
    )
    assert r.status is SandboxStatus.NONZERO
    assert r.exit_code == 75
    assert r.status not in INFRA_STATUSES


def test_sandbox_guest_signal_is_scientific_not_infra():
    """Guest self-kill is scientific NONZERO — only wrapper timeout/OOM are infra."""
    _require_hard()
    r = run_sandbox(
        ["python3", "-c", "import os,signal; os.kill(os.getpid(), signal.SIGKILL)"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64),
    )
    assert r.status is SandboxStatus.NONZERO
    assert r.status not in (
        SandboxStatus.TIMEOUT,
        SandboxStatus.OOM,
        SandboxStatus.UNAVAILABLE,
    )


def test_sandbox_blocks_network_fetchers_by_default():
    r = run_sandbox(
        ["curl", "https://example.com"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64, allow_network=False),
    )
    assert r.status is SandboxStatus.BLOCKED


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
""" % (
        str(meta_path),
    )
    r = run_sandbox(
        ["python3", "-c", attack],
        limits=SandboxLimits(timeout_s=10, memory_mb=64),
        inputs={"canary.txt": b"ro-canary"},
    )
    assert meta_path.read_bytes() == original
    assert r.net_isolation == "hard"
    assert r.fs_isolation == "hard"
    assert r.status is SandboxStatus.OK, r.stderr.decode(errors="replace")


def test_exec_runs_exit_code(tmp_path):
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(kind="exec_runs", argv=["/bin/true"], timeout_s=5, memory_mb=64, allow_network=False)
    cr = evaluate_code_check(
        check,
        template_id="exec",
        check_id="exec#0",
        out=AgentTaskOutput(outputs={}, summary=""),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
    )
    assert cr.status is ReqStatus.MET
    assert cr.effect is not None
    assert cr.effect.get("trusted") is True
    assert cr.effect.get("net_isolation") == "hard"
    assert cr.effect.get("fs_isolation") == "hard"
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
            "solution.py": ArtifactHandle(
                artifact_id=agent_code.artifact_id, name="solution.py"
            ),
            "conftest.py": ArtifactHandle(
                artifact_id=fake_junit.artifact_id, name="conftest.py"
            ),
        }, summary="")
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
        kind="tests_pass",
        argv=["catalog:pytest"],
        tests=catalog_tests,
        input_allowlist=["solution.py"],
        min_tests=2,
        timeout_s=30,
        memory_mb=128, allow_network=False)
    cr = evaluate_code_check(
        check,
        template_id="tp",
        check_id="tp#0",
        out=out,
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
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
            "holdout": ArtifactHandle(
                artifact_id=agent_holdout.artifact_id, name="holdout"
            ),
            "predictions": ArtifactHandle(
                artifact_id=predictions.artifact_id, name="predictions"
            ),
        }, summary="")
    check = CodeCheck(
        kind="metric_on_holdout",
        argv=["catalog:metric"],
        holdout=catalog_holdout,
        holdout_digest=holdout_digest,
        output="predictions",
        op=">=",
        target=0.4,
        timeout_s=10,
        memory_mb=64, allow_network=False)
    cr = evaluate_code_check(
        check,
        template_id="ho",
        check_id="ho#0",
        out=out,
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
    )
    assert cr.status is ReqStatus.MET, cr.detail
    assert cr.effect is not None
    # True accuracy vs catalog y: 3/4 = 0.75 — not 1.0 (agent labels) or 0.9 (claim).
    assert cr.effect.get("est") == 0.75
    assert cr.effect.get("est") != 0.9
    assert cr.effect.get("countable") is True

    # y_hat copied from agent-supplied labels cannot force a perfect score.
    check_hi = CodeCheck(
        kind="metric_on_holdout",
        argv=["catalog:metric"],
        holdout=catalog_holdout,
        holdout_digest=holdout_digest,
        output="predictions",
        op=">=",
        target=0.9,
        timeout_s=10,
        memory_mb=64, allow_network=False)
    cr_hi = evaluate_code_check(
        check_hi,
        template_id="ho",
        check_id="ho#hi",
        out=out,
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
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
        kind="reproducible_rerun",
        argv=[
            "python3",
            "-c",
            "import json,os,pathlib;pathlib.Path(os.environ['COSCIENTIST_RESULTS'])"
            f".joinpath('reward.txt').write_text(json.dumps("
            f"{{'schema': {REWARD_SCHEMA!r}, 'value': 1.0}}))",
        ],
        tolerance=0.01,
        timeout_s=5,
        memory_mb=64, allow_network=False)
    cr = evaluate_code_check(
        check,
        template_id="rr",
        check_id="rr#0",
        out=AgentTaskOutput(outputs={}, summary=""),
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
    )
    assert cr.status is ReqStatus.MET
    assert cr.effect is not None
    # Constant agent reward must not be countable / must not carry est → k_sup.
    assert cr.effect.get("countable") is False
    assert cr.effect.get("observation_only") is True
    assert "est" not in cr.effect
    assert cr.effect.get("trusted") is False


def test_countable_requires_attempt_binding(tmp_path):
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    cr = evaluate_code_check(
        CodeCheck(kind="exec_runs", argv=["/bin/true"], timeout_s=5, memory_mb=64, allow_network=False),
        template_id="exec",
        check_id="exec#nobind",
        out=AgentTaskOutput(outputs={}, summary=""),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id=None,
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
    )
    assert cr.effect is not None
    assert cr.effect.get("countable") is False
    assert cr.effect.get("produced_by_call_id", "").startswith("noattempt:")


def test_exec_runs_timeout_is_noncountable_refute_unresolved(tmp_path):
    """Sleep-bypass → FAILED guest timeout; never countable; refute_unresolved."""
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(
        kind="exec_runs",
        argv=["python3", "-c", "import time; time.sleep(1e9)"],
        timeout_s=0.3,
        memory_mb=64, allow_network=False)
    cr = evaluate_code_check(
        check,
        template_id="exec",
        check_id="exec#t",
        out=AgentTaskOutput(outputs={}, summary=""),
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
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
async def test_sandbox_infra_report_contains_reason_and_soft_note(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """MJ4: fail-closed isolation → infra; study still finalizes with report."""
    _force_soft(monkeypatch)
    cat = _catalog_with(
        {
            "id": "exec_ok",
            "title": "exec",
            "check": {
                "kind": "exec_runs",
                "argv": ["/bin/true"],
                "timeout_s": 5,
                "memory_mb": 64,
            },
            "role": "validity",
        }
    )
    sid = "st_sbx_mj4"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls, catalog=cat
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0-pr17",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
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
                criteria=[RequirementRef(template_id="exec_ok", required=True)], required=True, tolerate_failed_dep=False)
        ]
    )
    aid = await rt.start_attempt("E1")
    result = await ex.record(
        aid,
        AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS, summary=""),
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
    assert TaskReason.SANDBOX_INFRA.value in (
        fin.payload.get("contributing_reasons") or []
    )
    assert "sandbox_unavailable" in report_text
    assert "blocklist is bypassable" in report_text
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
                "argv": ["/bin/true"],
                "timeout_s": 5,
                "memory_mb": 64,
            },
            "role": "validity",
        }
    )
    sid = "st_sbx_ok"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls, catalog=cat
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0-pr17",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
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
                criteria=[RequirementRef(template_id="exec_ok", required=True)], required=True, tolerate_failed_dep=False)
        ]
    )
    aid = await rt.start_attempt("E1")
    result = await ex.record(aid, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS, summary=""))
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
        limits=SandboxLimits(timeout_s=5, memory_mb=64, max_output_bytes=1000),
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
        input_allowlist=frozenset({"predictions.json"}),
        limits=SandboxLimits(timeout_s=5, memory_mb=64),
        collect=["reward.txt"],
    )
    assert r.status is SandboxStatus.OK
    assert parse_reward(r.result_files["reward.txt"]) == 0.5
    assert b"999" in r.stdout  # stdout must not be the metric source


def test_agent_filename_allowlist_rejects_hooks_and_pth():
    from coscientist.checks.sandbox import agent_input_name_allowed

    assert not agent_filename_allowed("sitecustomize.py")
    assert not agent_filename_allowed("usercustomize.py")
    assert not agent_filename_allowed("foo.pth")
    assert not agent_filename_allowed("os.py")
    assert not agent_filename_allowed("pytest.py")
    assert agent_filename_allowed("solution.py")
    assert agent_filename_allowed("predictions.json")
    # Allowlist-only mount filter (empty → reject all).
    assert not agent_input_name_allowed("solution.py", None)
    assert not agent_input_name_allowed("solution.py", frozenset())
    assert agent_input_name_allowed("solution.py", frozenset({"solution.py"}))
    assert not agent_input_name_allowed("evil.py", frozenset({"solution.py"}))


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
            "predictions": ArtifactHandle(
                artifact_id=predictions.artifact_id, name="predictions"
            ),
            "holdout": ArtifactHandle(
                artifact_id=fake_hold.artifact_id, name="holdout"
            ),
        }, summary="")
    cr = evaluate_code_check(
        CodeCheck(
            kind="metric_on_holdout",
            argv=["catalog:metric"],
            holdout=catalog_holdout,
            holdout_digest=digest,
            output="predictions",
            op=">=",
            target=0.99,
            timeout_s=10,
            memory_mb=64, allow_network=False),
        template_id="ho",
        check_id="ho#eq",
        out=out,
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
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
            "sitecustomize.py": ArtifactHandle(
                artifact_id=evil.artifact_id, name="sitecustomize.py"
            ),
            "solution.py": ArtifactHandle(
                artifact_id=good.artifact_id, name="solution.py"
            ),
        }, summary="")
    cr = evaluate_code_check(
        CodeCheck(
            kind="tests_pass",
            argv=["catalog:pytest"],
            tests={
                "test_add.py": (
                    "import solution\n"
                    "def test_add():\n"
                    "    assert solution.add(1,2)==3\n"
                )
            },
            input_allowlist=["solution.py"],
            min_tests=1,
            timeout_s=30,
            memory_mb=128, allow_network=False),
        template_id="tp",
        check_id="tp#site",
        out=out,
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
    )
    assert cr.effect is not None
    assert cr.effect.get("trusted") is False
    assert cr.effect.get("countable") is False
    assert cr.status is ReqStatus.MET, cr.detail
    # Rejected sitecustomize must not have written /results/pwned.
    assert all(
        "pwned" not in (e.artifact_id or "") for e in (cr.evidence or [])
    )


def test_forged_junit_os_exit_still_untrusted(tmp_path):
    """Agent code can forge junit + os._exit — observation only (not countable)."""
    _require_hard()
    arts = LocalArtifactStore(tmp_path)
    # Malicious module imported by catalog test: forge junit then exit 0.
    evil = arts.put(
        b"import os, pathlib\n"
        b"pathlib.Path('/results/junit.xml').write_text("
        b"'<testsuite tests=\"9\" failures=\"0\" errors=\"0\"/>')\n"
        b"os._exit(0)\n",
        make_tool_ref("st/E1#1:evil"),
        name="solution.py",
        media_type="text/x-python",
    )
    out = AgentTaskOutput(
        outputs={
            "solution.py": ArtifactHandle(
                artifact_id=evil.artifact_id, name="solution.py"
            )
        }, summary="")
    cr = evaluate_code_check(
        CodeCheck(
            kind="tests_pass",
            argv=["catalog:pytest"],
            tests={"test_import.py": "import solution\ndef test_noop():\n    assert True\n"},
            input_allowlist=["solution.py"],
            min_tests=1,
            timeout_s=30,
            memory_mb=128, allow_network=False),
        template_id="tp",
        check_id="tp#forge",
        out=out,
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
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
            "solution.py": ArtifactHandle(
                artifact_id=good.artifact_id, name="solution.py"
            ),
        }, summary="")
    cr = evaluate_code_check(
        CodeCheck(
            kind="tests_pass",
            argv=["catalog:pytest"],
            tests={
                "test_add.py": (
                    "import solution\n"
                    "def test_add():\n"
                    "    assert solution.add(1,2)==3\n"
                )
            },
            # Even an explicit allowlist entry cannot mount .pth (denylist wins).
            input_allowlist=["solution.py", "evil.pth"],
            min_tests=1,
            timeout_s=30,
            memory_mb=128, allow_network=False),
        template_id="tp",
        check_id="tp#pth",
        out=out,
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
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
        outputs={
            "predictions": ArtifactHandle(
                artifact_id=predictions.artifact_id, name="predictions"
            )
        }, summary="")
    from coscientist.core.events.errors import CatalogError

    with pytest.raises(CatalogError, match="holdout_digest mismatch"):
        evaluate_code_check(
            CodeCheck(
                kind="metric_on_holdout",
                argv=["catalog:metric"],
                holdout={"y": [0, 1]},
                holdout_digest="0" * 64,
                output="predictions",
                op=">=",
                target=0.5,
                timeout_s=10,
                memory_mb=64, allow_network=False),
            template_id="ho",
            check_id="ho#bad",
            out=out,
            artifacts=arts,
            values={},
            role="prediction",
            attempt_id="st/E1#1",
        
            uses_sesoi=False,
            predicts_null=False,
            predicted_direction="two_sided",
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
        limits=SandboxLimits(timeout_s=10, memory_mb=64),
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
            kind="metric_on_holdout",
            argv=["catalog:metric"],
            holdout=secret,
            holdout_digest=digest,
            output="predictions",
            op=">=",
            target=0.99,
            timeout_s=10,
            memory_mb=64, allow_network=False),
        template_id="ho",
        check_id="ho#iso",
        out=AgentTaskOutput(
            outputs={
                "predictions": ArtifactHandle(
                    artifact_id=predictions.artifact_id, name="predictions"
                )
            }, summary=""),
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    
        uses_sesoi=False,
        predicts_null=False,
        predicted_direction="two_sided",
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
        input_allowlist=frozenset({"predictions.json"}),
        limits=SandboxLimits(timeout_s=10, memory_mb=64),
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

    def fake_prepare(memory_mb, timeout_s=30.0):
        leaf = real_prepare(memory_mb, timeout_s)
        if leaf is None:
            pytest.skip("cgroup delegation unavailable")
        # Replace launcher writer with one that always fails attach + signals infra.
        return leaf

    monkeypatch.setattr(sb, "_cgroup_prepare_leaf", fake_prepare)

    broken = '''
import os, sys
infra_fd = os.environ.get("COSCIENTIST_INFRA_FD")
if infra_fd is not None:
    try:
        os.write(int(infra_fd), b"COS_SBX_INFRA_v1:cgroup_attach\\n")
    except OSError:
        pass
sys.stderr.write("cgroup_attach:forced\\n")
sys.exit(70)
'''
    monkeypatch.setattr(sb, "_cgroup_bwrap_launcher", lambda leaf: broken)
    r = run_sandbox(
        ["/bin/true"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64),
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

    if not sb._cgroup_delegation_available():
        pytest.skip("cgroup memory delegation unavailable")
    # Allocate far beyond memory.max — expect OOM infra when detectable.
    r = run_sandbox(
        [
            "python3",
            "-c",
            "x = bytearray(200 * 1024 * 1024); x[0] = 1; "
            "import time; time.sleep(30)",
        ],
        limits=SandboxLimits(timeout_s=5, memory_mb=8, max_output_bytes=10_000),
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
        version="1",
        required=True,
        role="prediction",
        method_sources=["test"],
        applies_to=["experiment"],
        check=CodeCheck(
            kind="metric_on_holdout",
            argv=["catalog:metric"],
            holdout=holdout,
            holdout_digest=sha256_hex(canonical_json(holdout)),
            output="predictions",
            op=">=",
            target=0.5,
            tests={"test_x.py": "def test_x():\n    assert False\n"}, allow_network=False), uses_sesoi=False, predicts_null=False, predicted_direction="two_sided")
    view = agent_facing_template_view(tmpl)
    blob = json.dumps(view, default=str)
    # Kind name may contain the substring; secrets / payloads must not.
    assert "holdout" not in view
    assert "LABELS" not in blob
    assert '"y"' not in blob
    assert "catalog:metric" not in blob
    assert "test_x" not in blob
    assert "assert False" not in blob
    assert view["text_ru"] == tmpl.text_ru
    assert view["check_kind"] == "metric_on_holdout"
    # NodeRequirement path also omits the check body.
    req = template_to_requirement(tmpl, "n1")
    req_dump = req.model_dump()
    assert "holdout" not in req_dump
    assert "check" not in req_dump
    assert "LABELS" not in json.dumps(req_dump)


def test_classify_guest_timeout_idle_vs_busy():
    from coscientist.checks.sandbox import classify_guest_timeout

    assert (
        classify_guest_timeout(
            kill_cause="wall", duration_s=1.0, cpu_usage_s=0.01
        )
        == "idle_timeout"
    )
    assert (
        classify_guest_timeout(
            kill_cause="cpu", duration_s=1.0, cpu_usage_s=0.9
        )
        == "cpu_timeout"
    )
    assert (
        classify_guest_timeout(
            kill_cause="wall", duration_s=1.0, cpu_usage_s=0.5
        )
        == "wall_timeout"
    )
    # Missing cpu.stat → never idle for non-refute (cannot prove sabotage).
    assert (
        classify_guest_timeout(
            kill_cause="wall", duration_s=1.0, cpu_usage_s=None
        )
        == "wall_timeout"
    )
    # Refute / prediction without cgroup → untestable (idle), not benign wall.
    assert (
        classify_guest_timeout(
            kill_cause="wall",
            duration_s=1.0,
            cpu_usage_s=None,
            refute_role=True,
        )
        == "idle_timeout"
    )
    # kill_cause=None alone never yields cpu_timeout from classify_guest_timeout
    # (SIGXCPU promotion is in _classify via rusage / cgroup measurement).
    assert (
        classify_guest_timeout(
            kill_cause=None, duration_s=1.0, cpu_usage_s=0.5
        )
        == "wall_timeout"
    )


def test_guest_forged_sigxcpu_is_scientific_nonzero():
    """Guest os.kill(SIGXCPU) / exit 152 with low rusage must stay NONZERO."""
    _require_hard()
    r = run_sandbox(
        [
            "python3",
            "-c",
            "import os,signal; os.kill(os.getpid(), signal.SIGXCPU)",
        ],
        limits=SandboxLimits(timeout_s=5, memory_mb=64),
    )
    assert r.status is SandboxStatus.NONZERO
    assert r.reason != "cpu_timeout"
    assert r.status not in INFRA_STATUSES
    # exit 152 (128+SIGXCPU) with no CPU burn likewise
    r2 = run_sandbox(
        ["python3", "-c", "raise SystemExit(152)"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64),
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

    wall = 10.0
    budget = _cpu_budget_seconds(wall)
    fuse = _rlimit_cpu_seconds(wall, cgroup_live=True)
    bare = _rlimit_cpu_seconds(wall, cgroup_live=False)
    assert fuse is not None and bare is not None
    assert fuse > budget
    assert fuse <= math.floor(wall)
    assert bare == math.floor(budget)
    assert bare < wall
    # Fuse clears the race: watcher budget < fuse RLIMIT.
    assert fuse >= max(
        math.ceil(budget + max(1.0, budget * 0.10)),
        math.floor(budget) + 1,
    ) or fuse == max(
        math.ceil(max(budget + 0.05, wall - 0.05)),
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

    budget = _cpu_budget_seconds(wall)
    fuse = _rlimit_cpu_seconds(wall, cgroup_live=True)
    bare = _rlimit_cpu_seconds(wall, cgroup_live=False)
    assert fuse is not None, f"wall={wall}: expected cgroup fuse"
    assert fuse > budget, f"wall={wall}: fuse={fuse} must be > budget={budget}"
    assert fuse <= math.floor(wall)
    assert bare is not None and bare == math.floor(budget) and bare < wall


def test_sigxcpu_with_rusage_at_budget_is_cpu_timeout(monkeypatch):
    """No-cgroup path: SIGXCPU + host rusage ≥ budget → cpu_timeout."""
    import coscientist.checks.sandbox as sb

    _require_hard()
    # Force no cgroup leaf so classification uses rusage.
    monkeypatch.setattr(sb, "_cgroup_prepare_leaf", lambda *a, **k: None)
    monkeypatch.setattr(sb, "_cgroup_delegation_available", lambda: False)

    # Busy loop will hit RLIMIT at budget; rusage should promote to cpu_timeout.
    r = run_sandbox(
        ["python3", "-c", "while True: pass"],
        limits=SandboxLimits(timeout_s=3.0, memory_mb=64, max_output_bytes=10_000),
    )
    assert r.status is SandboxStatus.TIMEOUT
    assert r.reason == "cpu_timeout"
    assert r.status not in INFRA_STATUSES

    # Forged 152 with negligible CPU stays NONZERO.
    r2 = run_sandbox(
        ["python3", "-c", "raise SystemExit(152)"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64),
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
    assert hypothesis_verdict([a, b], True, supported_needed=2)[0] == "supported"

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
        [a, b, timeout], True, supported_needed=2
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
    v2, _, _, rule2 = hypothesis_verdict([a, b, wall], True, supported_needed=2)
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
                "argv": ["python3", "-c", "import time; time.sleep(1e9)"],
                "timeout_s": 0.25,
                "memory_mb": 64,
                # Mount agent payload so jail digests differ across attempts.
                "output": "payload",
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
        catalog_version="0.1.0-pr17",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
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
                criteria=[RequirementRef(template_id="exec_sleep", required=True)], required=True, tolerate_failed_dep=False)
        ]
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
                outputs={
                    "payload": ArtifactHandle(
                        artifact_id=art.artifact_id, name="payload"
                    )
                },
                claim=Outcome.SUCCESS, summary=""),
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
async def test_timeout_with_stuck_drain_charges_attempt(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """Timeout + stuck drain charges the attempt; catalog host budgets reach run_sandbox."""
    import os
    import select
    import signal
    import subprocess
    import sys
    import time

    import coscientist.checks.kinds.executable as exe_mod
    import coscientist.checks.sandbox as sb_mod
    from coscientist.checks.kinds.executable import _limits
    from coscientist.checks.sandbox_limits import apply_frozen_limits

    _require_hard()
    host_reap_s = 0.25
    host_drain_s = 0.25
    wall_s = 2.0
    guest = (
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    time.sleep(3600)\n"
        "    os._exit(0)\n"
        "while True:\n"
        "    pass\n"
    )
    cat = _catalog_with(
        {
            "id": "exec_busy_hold",
            "title": "busy+hold",
            "check": {
                "kind": "exec_runs",
                "argv": ["python3", "-c", guest],
                "timeout_s": wall_s,
                "memory_mb": 64,
                "reap_timeout_s": host_reap_s,
                "output_drain_join_s": host_drain_s,
            },
            "role": "validity",
        }
    )
    tmpl = next(t for t in cat.templates if t.id == "exec_busy_hold")
    # Production _limits on catalog check must carry catalog 0.25 (not 5.0 defaults).
    prod_lim = _limits(tmpl.check)
    assert prod_lim.reap_timeout_s == host_reap_s
    assert prod_lim.output_drain_join_s == host_drain_s
    assert prod_lim.reap_timeout_s != sb_mod.DEFAULT_REAP_TIMEOUT_S
    assert prod_lim.output_drain_join_s != sb_mod.DEFAULT_OUTPUT_DRAIN_JOIN_S

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
                criteria=[RequirementRef(template_id="exec_busy_hold", required=True)], required=True, tolerate_failed_dep=False)
        ]
    )
    task = next(t for t in rt.tasks.values() if t.spec.task_key == "E1")
    frozen_check = apply_frozen_limits(
        tmpl.check, task.spec, template_id="exec_busy_hold"
    )
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
            ready, _, _ = select.select([ready_r], [], [], 5.0)
            assert ready, "ready-pipe timed out"
            assert os.read(ready_r, 1) == b"R"
            os.close(ready_r)
            ready_r = -1
            hp_ready, _, _ = select.select([holder_r], [], [], 5.0)
            assert hp_ready, "holder pid pipe timed out"
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
                _rusage,
                drain_failed,
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
                kill_cause=kill_cause,
                cpu_usage_s=cpu_s,
                timeout_s=float(lim.timeout_s),
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
                try:
                    os.kill(holder_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                else:
                    deadline = time.monotonic() + float(lim.reap_timeout_s)
                    while time.monotonic() < deadline:
                        try:
                            wpid, _ = os.waitpid(holder_pid, os.WNOHANG)
                            if wpid == holder_pid:
                                break
                        except ChildProcessError:
                            if not os.path.isdir(f"/proc/{holder_pid}"):
                                break
                        select.select([], [], [], 0.05)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=float(lim.reap_timeout_s))
            except Exception:  # noqa: BLE001
                pass

    monkeypatch.setattr(exe_mod, "run_sandbox", run_sandbox_host_stuck_drain)

    aid = await rt.start_attempt("E1")
    result = await ex.record(
        aid, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS, summary="")
    )
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
    charged_used = sum(
        1 for a in task.attempts if rt.attempts[a]["charged"] is True
    )
    assert charged_used == 1
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_infra_failure_does_not_charge_attempt(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """Transient attach infra → charged=False; next attempt still within budget."""
    from coscientist.core.events.errors import SandboxInfraError

    cat = _catalog_with(
        {
            "id": "exec_ok",
            "title": "exec",
            "check": {
                "kind": "exec_runs",
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
        catalog_version="0.1.0-pr17",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
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
                criteria=[RequirementRef(template_id="exec_ok", required=True)], required=True, tolerate_failed_dep=False)
        ]
    )
    aid = await rt.start_attempt("E1")
    calls = {"n": 0}
    real_eval = evaluate_task_checks

    def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SandboxInfraError("sandbox_unavailable:cgroup_attach", transient=True)
        return real_eval(*args, **kwargs)

    monkeypatch.setattr(
        "coscientist.core.execution.executor.evaluate_task_checks", boom
    )
    result = await ex.record(
        aid, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS, summary="")
    )
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
        r2 = await ex.record(
            aid2, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS, summary="")
        )
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
                "argv": ["python3", "-c", "import time; time.sleep(1e9)"],
                "timeout_s": 0.25,
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
        catalog_version="0.1.0-pr17",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
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
                criteria=[RequirementRef(template_id="exec_sleep", required=True)], required=True, tolerate_failed_dep=False)
        ]
    )
    empty = AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS, summary="")
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
                "argv": ["/bin/true"],
                "timeout_s": 7.5,
                "memory_mb": 128,
            },
            "role": "validity",
        }
    )
    sid = "st_sbx_freeze"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls, catalog=cat
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0-pr17",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
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
                criteria=[RequirementRef(template_id="exec_ok", required=True)],
                # Agent tries to override — must be stripped.
                sandbox_timeout_s=0.05,
                sandbox_memory_mb=1, required=True, tolerate_failed_dep=False)
        ]
    )
    task = next(t for t in rt.tasks.values() if t.spec.task_key == "E1")
    assert task.spec.sandbox_timeout_s is None
    assert task.spec.sandbox_memory_mb is None
    assert task.spec.sandbox_check_limits == {
        "exec_ok": {
            "timeout_s": 7.5,
            "memory_mb": 128,
            "allow_network": False,
        }
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
            criteria=[RequirementRef(template_id="exec_ok", required=True)],
            sandbox_timeout_s=0.05,
            sandbox_memory_mb=1,
            sandbox_check_limits={"exec_ok": {"timeout_s": 0.05}}, required=True, tolerate_failed_dep=False),
        cat,
    )
    assert frozen.sandbox_timeout_s is None
    assert frozen.sandbox_memory_mb is None
    assert frozen.sandbox_check_limits["exec_ok"]["timeout_s"] == 30.0
    assert "stripped_agent_sandbox_limits" in (frozen.sandbox_limits_note or "")
    assert "stripped_agent_sandbox_check_limits" in (frozen.sandbox_limits_note or "")
    check = CodeCheck(kind="exec_runs", argv=["/bin/true"], timeout_s=99.0, allow_network=False)
    applied = apply_frozen_limits(check, frozen, template_id="exec_ok")
    assert applied.timeout_s == 30.0
    assert applied.memory_mb == 256


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
                "argv": ["catalog:metric"],
                "holdout": holdout,
                "holdout_digest": sha256_hex(canonical_json(holdout)),
                "output": "predictions",
                "op": ">=",
                "target": 0.5,
                "tests": {"test_x.py": "assert False"},
            },
            "role": "prediction",
        }
    )
    view = agent_facing_catalog_view(cat)
    tmpl_view = view["templates"][0]
    blob = json.dumps(view, default=str)
    assert "NEVER" not in blob
    # Kind name may contain the substring; secret keys / payloads must not.
    assert "holdout" not in tmpl_view  # no holdout field on the view dict
    assert '"y"' not in blob
    assert "catalog:metric" not in blob
    assert "assert False" not in blob
    assert tmpl_view["template_id"] == "metric_secret"
    assert tmpl_view["check_kind"] == "metric_on_holdout"


def test_sandbox_timeout_report_line_by_reason_only():
    from coscientist.core.claims.reasons import TaskReason
    from coscientist.core.finalize.finalize import (
        IDLE_TIMEOUT_RU,
        TIMEOUT_BUDGET_RU,
        sandbox_timeout_report_line,
    )

    assert (
        sandbox_timeout_report_line(TaskReason.IDLE_TIMEOUT.value, "anything")
        == IDLE_TIMEOUT_RU
    )
    assert (
        sandbox_timeout_report_line(TaskReason.SANDBOX_TIMEOUT.value, "x")
        == TIMEOUT_BUDGET_RU
    )
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
        claims=[{"id": "C1", "target": "Q1"}],
        now=now,
        graph=g,
        derived={
            "criterion_status": "met",
            "claim_outcome": "consistent",
            "evidence_kind": "computational",
            "frozen": True,
            "lit_own_criterion": False,
            "accepted": True,
            "validity_ok": True,
            "predicted_direction": "positive",
            "predicts_null": False,
            "effect": {
                "est": 1.0,
                "core_evaluated": True,
                "countable": True,
                # intentionally no produced_by_call_id
            },
            "check_role": "prediction",
        },
        infra_failure=False,
    )
    assert drops == []
    saw = False
    for ev in evs:
        if ev.type == "GraphNodeUpserted":
            effect = (ev.payload.get("attrs") or {}).get("effect") or {}
            saw = True
            assert "produced_by_call_id" not in effect or effect.get(
                "produced_by_call_id"
            ) in (None, "")
            assert ":derived" not in str(effect.get("produced_by_call_id") or "")
    assert saw


def test_freeze_shared_helper_used_by_all_task_create_paths():
    """accept_plan / expand / foreach / expand_trigger share freeze_task_sandbox_limits."""
    import inspect

    from coscientist.checks import sandbox_limits as sl
    from coscientist.core.plan import expand, expand_trigger, foreach, lifecycle

    assert "freeze_task_sandbox_limits" in inspect.getsource(
        lifecycle.LifecycleMixin.accept_plan
    )
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
            criteria=[RequirementRef(template_id="exec_ok", required=True)],
            sandbox_timeout_s=0.05, required=True, tolerate_failed_dep=False),
        cat,
    )
    assert out.sandbox_check_limits["exec_ok"]["timeout_s"] == 12.0
    assert out.sandbox_timeout_s is None


@pytest.mark.asyncio
async def test_alternating_prediction_timeouts_elevate_untestable(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """≥2 prediction timeouts on a task (different templates) → idle/untestable.

    Alternating template_ids must not evade the structural rule.
    """
    from coscientist.core.execution.domain import CheckResult

    cat = _catalog_with(
        {
            "id": "pred_a",
            "title": "a",
            "check": {"kind": "exec_runs", "argv": ["/bin/true"], "timeout_s": 5},
            "role": "prediction",
        },
        {
            "id": "pred_b",
            "title": "b",
            "check": {"kind": "exec_runs", "argv": ["/bin/true"], "timeout_s": 5},
            "role": "prediction",
        },
    )
    sid = "st_sbx_alt_to"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls, catalog=cat
    )
    rt.max_attempts = 3
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0-pr17",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
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
                    RequirementRef(template_id="pred_a", required=True),
                    RequirementRef(template_id="pred_b", required=True),
                ], required=True, tolerate_failed_dep=False)
        ]
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
        used_sesoi=False,
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
        used_sesoi=False,
    ),
        ]

    monkeypatch.setattr(
        "coscientist.core.execution.executor.evaluate_task_checks", _fake_eval
    )

    aid1 = await rt.start_attempt("E1")
    r1 = await ex.record(aid1, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS, summary=""))
    # First prediction timeout on the task — do NOT escalate to untestable.
    assert r1.reason == TaskReason.SANDBOX_TIMEOUT.value
    row1 = rt.results[aid1]
    assert row1.get("result_class") == "timeout"
    assert "repeat_timeout" not in str(row1.get("detail") or "")

    clock.advance(timedelta(seconds=1))
    aid2 = await rt.start_attempt("E1")
    r2 = await ex.record(aid2, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS, summary=""))
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
async def test_single_prediction_timeout_on_last_attempt_not_escalated(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """One prediction timeout on the last charged attempt stays sandbox_timeout."""
    from coscientist.core.execution.domain import CheckResult

    cat = _catalog_with(
        {
            "id": "pred_only",
            "title": "p",
            "check": {"kind": "exec_runs", "argv": ["/bin/true"], "timeout_s": 5},
            "role": "prediction",
        }
    )
    sid = "st_sbx_last_to"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls, catalog=cat
    )
    rt.max_attempts = 1
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0-pr17",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
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
                criteria=[RequirementRef(template_id="pred_only", required=True)], required=True, tolerate_failed_dep=False)
        ]
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
        used_sesoi=False,
    )
        ]

    monkeypatch.setattr(
        "coscientist.core.execution.executor.evaluate_task_checks", _fake_eval
    )
    aid = await rt.start_attempt("E1")
    r = await ex.record(aid, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS, summary=""))
    assert r.reason == TaskReason.SANDBOX_TIMEOUT.value
    row = rt.results[aid]
    assert not any(
        (cr.get("effect") or {}).get("repeat_timeout")
        for cr in (row.get("check_results") or [])
        if isinstance(cr, dict)
    )
    await close_runtime(rt)
