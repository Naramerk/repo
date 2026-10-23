"""M1 PR17: executable kinds + bwrap sandbox / separate verifier."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from coscientist.checks.catalog import load_catalog
from coscientist.checks.evaluate import evaluate_code_check
from coscientist.checks.sandbox import SandboxLimits, SandboxStatus, run_sandbox, run_verifier
from coscientist.checks.sandbox_probe import bwrap_available, probe_bwrap_hard
from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    CodeCheck,
    Outcome,
    ReqStatus,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.errors import SandboxInfraError
from coscientist.core.reasons import TaskReason
from tests.helpers import attach_executor, close_runtime, open_runtime, record_success

# Scorer that writes reward.txt from predictions (catalog verifier; no agent code).
_METRIC_SCORER = b"""
import json, os, pathlib
results = pathlib.Path(os.environ["COSCIENTIST_RESULTS"])
inputs = pathlib.Path(os.environ["COSCIENTIST_INPUTS"])
doc = json.loads((inputs / "predictions.json").read_bytes())
(results / "reward.txt").write_text(str(float(doc["score"])))
(results / "metrics.json").write_bytes((inputs / "predictions.json").read_bytes())
"""

_JUNIT_SCORER = b"""
import os, pathlib
results = pathlib.Path(os.environ["COSCIENTIST_RESULTS"])
(results / "junit.xml").write_text(
    '<testsuite tests="2" failures="0" errors="0"/>'
)
"""

_RERUN_SCORER = b"""
import os, pathlib
results = pathlib.Path(os.environ["COSCIENTIST_RESULTS"])
(results / "reward.txt").write_text("1.0")
"""


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
            "catalog_version": "0.1.0-pr17",
            "edition": "pr17",
            "rules": rules,
            "templates": [
                {
                    **t,
                    "text_ru": t.get("text_ru", "проверка"),
                    "rule_ids": ["R23"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "role": t.get("role", "validity"),
                    "required": True,
                }
                for t in templates
            ],
            "defaults": {"experiment": [templates[0]["id"]]},
        }
    )


def _force_soft(monkeypatch):
    """Soft path: no fake hard — probe fails (bwrap may still be present)."""
    import coscientist.checks.sandbox_probe as probe

    probe.probe_bwrap_hard.cache_clear()
    monkeypatch.setattr(
        probe,
        "probe_bwrap_hard",
        lambda *, allow_network=False: False,
    )


def test_sandbox_ok_exit_zero():
    r = run_sandbox(["/bin/true"], limits=SandboxLimits(timeout_s=5, memory_mb=64))
    assert r.status is SandboxStatus.OK
    assert r.exit_code == 0
    assert r.limits_fully_held == (
        r.net_isolation == "hard" and r.fs_isolation == "hard"
    )


def test_sandbox_hard_isolation_when_bwrap_available():
    """Real bwrap probe — never fake hard with an empty prefix."""
    if not bwrap_available() or not probe_bwrap_hard(allow_network=False):
        pytest.fail("bubblewrap hard probe must pass in CI (apt install bubblewrap)")
    r = run_sandbox(
        ["/bin/true"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64, allow_network=False),
    )
    assert r.status is SandboxStatus.OK
    assert r.net_isolation == "hard"
    assert r.fs_isolation == "hard"
    assert r.limits_fully_held is True


def test_sandbox_soft_when_bwrap_probe_fails(monkeypatch, caplog, tmp_path):
    import logging

    _force_soft(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="coscientist.checks.sandbox_probe"):
        r = run_sandbox(
            ["/bin/true"],
            limits=SandboxLimits(timeout_s=5, memory_mb=64, allow_network=False),
        )
    assert r.status is SandboxStatus.OK
    assert r.net_isolation == "soft"
    assert r.fs_isolation == "soft"
    assert r.limits_fully_held is False

    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(
        kind="exec_runs", argv=["/bin/true"], timeout_s=5, memory_mb=64
    )
    cr = evaluate_code_check(
        check,
        template_id="exec",
        check_id="exec#soft",
        out=AgentTaskOutput(outputs={}),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.MET
    assert cr.effect is not None
    assert cr.effect.get("trusted") is False
    assert cr.effect.get("countable") is True
    assert cr.detail and "blocklist is bypassable" in cr.detail
    assert "FS not mount-isolated" in (cr.detail or "")


def test_sandbox_no_bwrap_not_trusted(monkeypatch, tmp_path):
    import coscientist.checks.sandbox_probe as probe

    probe._bwrap_bin.cache_clear()
    probe.probe_bwrap_hard.cache_clear()
    monkeypatch.setattr(probe, "_bwrap_bin", lambda: None)
    monkeypatch.setattr(probe, "bwrap_available", lambda: False)
    r = run_sandbox(["/bin/true"], limits=SandboxLimits(timeout_s=5, memory_mb=64))
    assert r.limits_fully_held is False
    assert r.fs_isolation == "soft"
    arts = LocalArtifactStore(tmp_path)
    cr = evaluate_code_check(
        CodeCheck(kind="exec_runs", argv=["/bin/true"], timeout_s=5, memory_mb=64),
        template_id="exec",
        check_id="exec#nobwrap",
        out=AgentTaskOutput(outputs={}),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr.effect is not None
    assert cr.effect.get("trusted") is False
    assert "bubblewrap" in (cr.detail or cr.effect.get("note") or "")


def test_sandbox_allow_network_not_trusted(tmp_path):
    if not bwrap_available():
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
        out=AgentTaskOutput(outputs={}),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.MET
    assert cr.effect is not None
    assert cr.effect.get("net_isolation") == "none"
    assert cr.effect.get("trusted") is False


def test_sandbox_timeout_is_infra_status():
    r = run_sandbox(
        ["python3", "-c", "import time; time.sleep(30)"],
        limits=SandboxLimits(timeout_s=0.3, memory_mb=64, max_output_bytes=10_000),
    )
    assert r.status is SandboxStatus.TIMEOUT
    assert r.reason == "sandbox_timeout"


def test_sandbox_positive_exit_137_is_scientific_not_oom():
    r = run_sandbox(
        ["python3", "-c", "raise SystemExit(137)"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64),
    )
    assert r.status is SandboxStatus.NONZERO
    assert r.exit_code == 137


def test_sandbox_guest_signal_is_scientific_not_infra():
    """Guest self-kill is scientific NONZERO — only wrapper timeout/OOM are infra."""
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
    """Hard bwrap: host meta is not mounted — guest cannot rewrite it (B2)."""
    if not bwrap_available() or not probe_bwrap_hard(allow_network=False):
        pytest.fail("bubblewrap hard probe must pass in CI")
    arts = LocalArtifactStore(tmp_path)
    art = arts.put(
        b"secret",
        make_tool_ref("st/E1#1:data"),
        name="data.bin",
        media_type="application/octet-stream",
    )
    meta_path = arts.meta / f"{art.artifact_id}.json"
    original = meta_path.read_bytes()
    r = run_sandbox(
        [
            "python3",
            "-c",
            f"open({str(meta_path)!r},'w').write('pwned')",
        ],
        limits=SandboxLimits(timeout_s=5, memory_mb=64),
    )
    # Write either fails (path missing in jail) or is a no-op on host.
    assert meta_path.read_bytes() == original
    assert r.net_isolation == "hard"
    assert r.fs_isolation == "hard"


def test_exec_runs_exit_code(tmp_path):
    if not probe_bwrap_hard(allow_network=False):
        pytest.fail("bubblewrap hard probe required")
    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(kind="exec_runs", argv=["/bin/true"], timeout_s=5, memory_mb=64)
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
    assert cr.effect.get("trusted") is True
    assert cr.effect.get("net_isolation") == "hard"
    assert cr.effect.get("fs_isolation") == "hard"
    assert cr.effect.get("countable") is True
    assert cr.effect.get("produced_by_call_id", "").startswith("st/E1#1:")


def test_tests_pass_via_verifier(tmp_path):
    """Catalog scorer in verifier jail writes junit; agent junit ignored."""
    arts = LocalArtifactStore(tmp_path)
    # Agent tries to submit fake junit — must not be trusted as the source.
    fake = arts.put(
        b'<testsuite tests="99" failures="0"/>',
        make_tool_ref("st/E1#1:junit"),
        name="junit.xml",
        media_type="application/xml",
    )
    out = AgentTaskOutput(
        outputs={"code": ArtifactHandle(artifact_id=fake.artifact_id, name="code")}
    )
    scorer = tmp_path / "scorer.py"
    scorer.write_bytes(_JUNIT_SCORER)
    check = CodeCheck(
        kind="tests_pass",
        argv=["python3", str(scorer)],
        min_tests=2,
        timeout_s=5,
        memory_mb=64,
    )
    # run_verifier binds scorer via scorer_bytes — pass catalog: use inline bytes path
    # by using catalog metric-style: put scorer content through argv that reads env.
    # Simpler: argv uses python -c writing junit to RESULTS.
    check = CodeCheck(
        kind="tests_pass",
        argv=[
            "python3",
            "-c",
            "import os,pathlib;p=pathlib.Path(os.environ['COSCIENTIST_RESULTS']);"
            "p.joinpath('junit.xml').write_text("
            "'<testsuite tests=\"2\" failures=\"0\" errors=\"0\"/>')",
        ],
        min_tests=2,
        timeout_s=5,
        memory_mb=64,
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
    assert cr.status is ReqStatus.MET
    assert cr.effect and cr.effect.get("producer") == "sandbox"


def test_holdout_digest_frozen_fake_agent_metric_not_trusted(tmp_path):
    """Fake agent metrics ignored; verifier computes reward from predictions (B1)."""
    arts = LocalArtifactStore(tmp_path)
    holdout = arts.put(
        b"holdout-bytes",
        make_tool_ref("st/E1#1:holdout"),
        name="holdout.bin",
        media_type="application/octet-stream",
    )
    # Agent predictions (true input) + fake high "metrics" must not win.
    predictions = arts.put(
        json.dumps({"score": 0.9}).encode(),
        make_tool_ref("st/E1#1:pred"),
        name="predictions.json",
        media_type="application/json",
    )
    fake_metrics = arts.put(
        json.dumps({"score": 0.99}).encode(),
        make_tool_ref("st/E1#1:metrics"),
        name="metrics.json",
        media_type="application/json",
    )
    out = AgentTaskOutput(
        outputs={
            "holdout": ArtifactHandle(artifact_id=holdout.artifact_id, name="holdout"),
            "predictions": ArtifactHandle(
                artifact_id=predictions.artifact_id, name="predictions"
            ),
            "metrics": ArtifactHandle(
                artifact_id=fake_metrics.artifact_id, name="metrics"
            ),
        }
    )
    check = CodeCheck(
        kind="metric_on_holdout",
        argv=["catalog:metric"],
        holdout_output="holdout",
        output="predictions",
        holdout_digest=holdout.sha256,
        metric="score",
        pointer="/score",
        op=">=",
        target=0.5,
        timeout_s=5,
        memory_mb=64,
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
    assert cr.status is ReqStatus.MET
    assert cr.effect is not None
    assert cr.effect.get("est") == 0.9
    assert cr.effect.get("est") != 0.99
    assert cr.effect.get("countable") is True
    assert cr.effect.get("produced_by_call_id", "").startswith("st/E1#1:")

    other = arts.put(
        b"other-holdout",
        make_tool_ref("st/E1#1:holdout2"),
        name="holdout2.bin",
        media_type="application/octet-stream",
    )
    out_bad = AgentTaskOutput(
        outputs={
            "holdout": ArtifactHandle(artifact_id=other.artifact_id, name="holdout"),
            "predictions": ArtifactHandle(
                artifact_id=predictions.artifact_id, name="predictions"
            ),
        }
    )
    cr2 = evaluate_code_check(
        check,
        template_id="ho",
        check_id="ho#1",
        out=out_bad,
        artifacts=arts,
        values={},
        role="prediction",
        attempt_id="st/E1#1",
    )
    assert cr2.status is ReqStatus.FAILED
    assert cr2.detail == "holdout_digest_mismatch"


def test_reproducible_rerun_compares_result_digests(tmp_path):
    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(
        kind="reproducible_rerun",
        argv=[
            "python3",
            "-c",
            "import os,pathlib;pathlib.Path(os.environ['COSCIENTIST_RESULTS'])"
            ".joinpath('reward.txt').write_text('1.0')",
        ],
        tolerance=0.01,
        timeout_s=5,
        memory_mb=64,
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
    assert cr.effect.get("countable") is True


def test_countable_requires_attempt_binding(tmp_path):
    arts = LocalArtifactStore(tmp_path)
    cr = evaluate_code_check(
        CodeCheck(kind="exec_runs", argv=["/bin/true"], timeout_s=5, memory_mb=64),
        template_id="exec",
        check_id="exec#nobind",
        out=AgentTaskOutput(outputs={}),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id=None,
    )
    assert cr.effect is not None
    assert cr.effect.get("countable") is False
    assert cr.effect.get("produced_by_call_id", "").startswith("noattempt:")


def test_exec_runs_timeout_raises_infra(tmp_path):
    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(
        kind="exec_runs",
        argv=["python3", "-c", "import time; time.sleep(30)"],
        timeout_s=0.3,
        memory_mb=64,
    )
    with pytest.raises(SandboxInfraError, match="sandbox_timeout"):
        evaluate_code_check(
            check,
            template_id="exec",
            check_id="exec#t",
            out=AgentTaskOutput(outputs={}),
            artifacts=arts,
            values={},
            role="validity",
            attempt_id="st/E1#1",
        )


@pytest.mark.asyncio
async def test_sandbox_infra_report_contains_reason_and_soft_note(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """MJ4: finalize without cancelling REPORT; strict asserts (no 'or')."""
    _force_soft(monkeypatch)
    cat = _catalog_with(
        {
            "id": "exec_ok",
            "title": "exec",
            "check": {
                "kind": "exec_runs",
                "argv": ["python3", "-c", "import time; time.sleep(30)"],
                "timeout_s": 0.3,
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
                criteria=[RequirementRef(template_id="exec_ok")],
            )
        ]
    )
    aid = await rt.start_attempt("E1")
    result = await ex.record(
        aid,
        AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS),
    )
    assert result.outcome is Outcome.FAILED
    assert result.reason == TaskReason.SANDBOX_INFRA.value
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
    assert "sandbox_timeout" in report_text
    assert "blocklist is bypassable" in report_text
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_exec_runs_production_path_trusted(tmp_path, clock, runtime_cls):
    if not probe_bwrap_hard(allow_network=False):
        pytest.fail("bubblewrap hard probe required in CI")
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
                criteria=[RequirementRef(template_id="exec_ok")],
            )
        ]
    )
    aid = await rt.start_attempt("E1")
    result = await ex.record(aid, AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS))
    assert result.outcome is Outcome.SUCCESS
    sandbox_arts = [a for a in arts._index.values() if a.producer == "sandbox"]
    assert sandbox_arts
    assert all(a.net_isolation == "hard" for a in sandbox_arts)
    assert all(a.fs_isolation == "hard" for a in sandbox_arts)
    await close_runtime(rt)


def test_streaming_output_cap_kills(monkeypatch):
    _force_soft(monkeypatch)
    r = run_sandbox(
        ["python3", "-c", "print('x'*100000)"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64, max_output_bytes=1000),
    )
    assert r.status is SandboxStatus.OUTPUT_EXCEEDED
    assert len(r.stdout) <= 1000


def test_verifier_reads_reward_not_stdout():
    r = run_verifier(
        [
            "python3",
            "-c",
            "import os,pathlib; print('999'); "
            "pathlib.Path(os.environ['COSCIENTIST_RESULTS'])"
            ".joinpath('reward.txt').write_text('0.5')",
        ],
        inputs={"predictions.json": b'{"score": 0.5}'},
        limits=SandboxLimits(timeout_s=5, memory_mb=64),
        collect=["reward.txt"],
    )
    assert r.status is SandboxStatus.OK
    assert r.result_files.get("reward.txt") == b"0.5"
    assert b"999" in r.stdout  # stdout must not be the metric source
