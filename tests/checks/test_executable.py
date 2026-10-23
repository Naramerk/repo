"""M1 PR17: executable CodeCheck kinds + sandbox limits (B7, B8, K5)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from coscientist.checks.catalog import load_catalog
from coscientist.checks.evaluate import evaluate_code_check
from coscientist.checks.sandbox import SandboxLimits, SandboxStatus, run_sandbox
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


def _force_hard(monkeypatch):
    monkeypatch.setattr(
        "coscientist.checks.sandbox.resolve_isolation",
        lambda *, allow_network=False: (
            ("none", "hard", []) if allow_network else ("hard", "hard", [])
        ),
    )


def _force_soft(monkeypatch):
    monkeypatch.setattr(
        "coscientist.checks.sandbox.resolve_isolation",
        lambda *, allow_network=False: (
            ("none", "soft", []) if allow_network else ("soft", "soft", [])
        ),
    )


def test_sandbox_ok_exit_zero():
    r = run_sandbox(["/bin/true"], limits=SandboxLimits(timeout_s=5, memory_mb=64))
    assert r.status is SandboxStatus.OK
    assert r.exit_code == 0
    assert r.net_isolation in ("hard", "soft")
    assert r.fs_isolation in ("hard", "soft")
    assert r.limits_fully_held == (
        r.net_isolation == "hard" and r.fs_isolation == "hard"
    )


def test_sandbox_hard_isolation_when_unshare_available(monkeypatch):
    _force_hard(monkeypatch)
    r = run_sandbox(
        ["/bin/true"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64, allow_network=False),
    )
    assert r.status is SandboxStatus.OK
    assert r.net_isolation == "hard"
    assert r.fs_isolation == "hard"
    assert r.limits_fully_held is True


def test_sandbox_soft_isolation_when_unshare_forbidden(monkeypatch, caplog, tmp_path):
    """Soft isolation → trusted=False; note says network may be open (MJ5)."""
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
    meta = arts.get_meta(cr.evidence[0].artifact_id)
    assert meta is not None
    assert meta.producer == "sandbox"
    assert meta.net_isolation == "soft"
    assert meta.fs_isolation == "soft"
    assert cr.effect is not None
    assert cr.effect.get("trusted") is False
    assert cr.effect.get("countable") is True
    assert cr.effect.get("net_isolation") == "soft"
    assert cr.detail and "blocklist is bypassable" in cr.detail
    assert cr.effect.get("note") and "network may be open" in cr.effect["note"]

    r2 = run_sandbox(
        ["python3", "-c", "import time; time.sleep(5)"],
        limits=SandboxLimits(timeout_s=0.25, memory_mb=64),
    )
    assert r2.status is SandboxStatus.TIMEOUT
    assert r2.net_isolation == "soft"


def test_sandbox_allow_network_not_trusted(monkeypatch, tmp_path):
    """allow_network=True ⇒ net=none ⇒ trusted=False (MJ3)."""
    _force_hard(monkeypatch)
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
    """sys.exit(137) must stay NONZERO, not OOM (B3)."""
    r = run_sandbox(
        ["python3", "-c", "raise SystemExit(137)"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64),
    )
    assert r.status is SandboxStatus.NONZERO
    assert r.exit_code == 137


def test_sandbox_blocks_network_fetchers_by_default():
    r = run_sandbox(
        ["curl", "https://example.com"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64, allow_network=False),
    )
    assert r.status is SandboxStatus.BLOCKED
    r2 = run_sandbox(
        ["curl", "https://example.com"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64, allow_network=True),
    )
    assert r2.status is not SandboxStatus.BLOCKED


def test_sandbox_cannot_rewrite_artifact_meta(tmp_path, monkeypatch):
    """Hard FS: guest cannot rewrite artifact meta (B2)."""
    _force_hard(monkeypatch)
    # Use real unshare when available so protect_paths tmpfs actually applies.
    from coscientist.checks.sandbox_probe import probe_unshare

    if not probe_unshare("--user", "--map-root-user", "--mount", "--net"):
        pytest.skip("hard unshare unavailable on this host")
    monkeypatch.undo()  # restore real resolve_isolation

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
        protect_paths=[str(arts.meta), str(arts.cas), str(arts.root)],
    )
    assert r.status in (
        SandboxStatus.OK,
        SandboxStatus.NONZERO,
        SandboxStatus.CRASH,
    )
    assert meta_path.read_bytes() == original


def test_exec_runs_exit_code(tmp_path, monkeypatch):
    _force_hard(monkeypatch)
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
    assert cr.evidence
    meta = arts.get_meta(cr.evidence[0].artifact_id)
    assert meta is not None
    assert meta.producer == "sandbox"
    assert meta.net_isolation == "hard"
    assert meta.fs_isolation == "hard"
    assert cr.effect is not None
    assert cr.effect.get("trusted") is True
    assert cr.effect.get("net_isolation") == "hard"
    assert cr.effect.get("fs_isolation") == "hard"

    check_fail = CodeCheck(
        kind="exec_runs", argv=["/bin/false"], timeout_s=5, memory_mb=64
    )
    cr2 = evaluate_code_check(
        check_fail,
        template_id="exec",
        check_id="exec#1",
        out=AgentTaskOutput(outputs={}),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr2.status is ReqStatus.FAILED


def test_tests_pass_junit(tmp_path, monkeypatch):
    """Core runs argv; junit read only from core-owned path (B1)."""
    _force_hard(monkeypatch)
    arts = LocalArtifactStore(tmp_path)
    # Guest writes junit into $COSCIENTIST_SANDBOX_CORE/junit.xml
    argv = [
        "python3",
        "-c",
        "import os,pathlib;"
        "p=pathlib.Path(os.environ['COSCIENTIST_SANDBOX_CORE'])/'junit.xml';"
        "p.write_text('<testsuite tests=\"2\" failures=\"0\" errors=\"0\"/>')",
    ]
    check = CodeCheck(kind="tests_pass", argv=argv, min_tests=2, timeout_s=5, memory_mb=64)
    cr = evaluate_code_check(
        check,
        template_id="tp",
        check_id="tp#0",
        out=AgentTaskOutput(outputs={}),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.MET
    assert cr.effect and cr.effect.get("trusted") is True
    assert cr.effect.get("producer") == "sandbox"

    argv_fail = [
        "python3",
        "-c",
        "import os,pathlib;"
        "p=pathlib.Path(os.environ['COSCIENTIST_SANDBOX_CORE'])/'junit.xml';"
        "p.write_text('<testsuite tests=\"1\" failures=\"1\" errors=\"0\"/>')",
    ]
    check_fail = CodeCheck(
        kind="tests_pass", argv=argv_fail, min_tests=1, timeout_s=5, memory_mb=64
    )
    cr2 = evaluate_code_check(
        check_fail,
        template_id="tp",
        check_id="tp#1",
        out=AgentTaskOutput(outputs={}),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr2.status is ReqStatus.FAILED


def test_tests_pass_agent_junit_without_argv_not_trusted(tmp_path):
    """Without catalog argv, agent-submitted junit is not trusted (B1)."""
    arts = LocalArtifactStore(tmp_path)
    junit = b"""<?xml version="1.0"?>
<testsuite name="t" tests="2" failures="0" errors="0"></testsuite>
"""
    art = arts.put(
        junit,
        make_tool_ref("st/E1#1:junit"),
        name="junit.xml",
        media_type="application/xml",
    )
    out = AgentTaskOutput(
        outputs={"junit": ArtifactHandle(artifact_id=art.artifact_id, name="junit")}
    )
    check = CodeCheck(kind="tests_pass", output="junit", min_tests=2)
    cr = evaluate_code_check(
        check,
        template_id="tp",
        check_id="tp#agent",
        out=out,
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.FAILED
    assert cr.effect is not None
    assert cr.effect.get("trusted") is False
    assert cr.effect.get("producer") == "core"


def test_holdout_digest_frozen(tmp_path, monkeypatch):
    """Fake agent metrics are NOT trusted; core-owned sandbox metrics win (B1)."""
    _force_hard(monkeypatch)
    arts = LocalArtifactStore(tmp_path)
    holdout = arts.put(
        b"holdout-bytes",
        make_tool_ref("st/E1#1:holdout"),
        name="holdout.bin",
        media_type="application/octet-stream",
    )
    # Agent lies with a perfect score — must be ignored.
    fake_metrics = arts.put(
        json.dumps({"score": 0.99}).encode(),
        make_tool_ref("st/E1#1:metrics"),
        name="metrics.json",
        media_type="application/json",
    )
    out = AgentTaskOutput(
        outputs={
            "holdout": ArtifactHandle(artifact_id=holdout.artifact_id, name="holdout"),
            "metrics": ArtifactHandle(
                artifact_id=fake_metrics.artifact_id, name="metrics"
            ),
        }
    )
    # Catalog metric script writes the real (lower) score to core/metrics.json.
    argv = [
        "python3",
        "-c",
        "import json,os,pathlib;"
        "core=pathlib.Path(os.environ['COSCIENTIST_SANDBOX_CORE']);"
        "(core/'metrics.json').write_text(json.dumps({'score': 0.9}))",
    ]
    check = CodeCheck(
        kind="metric_on_holdout",
        argv=argv,
        holdout_output="holdout",
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
    assert cr.effect.get("trusted") is True
    assert cr.effect.get("producer") == "sandbox"
    assert cr.effect.get("est") == 0.9  # not agent 0.99

    # Drifted holdout → scientific FAILED (digest mismatch), not infra.
    other = arts.put(
        b"other-holdout",
        make_tool_ref("st/E1#1:holdout2"),
        name="holdout2.bin",
        media_type="application/octet-stream",
    )
    out_bad = AgentTaskOutput(
        outputs={
            "holdout": ArtifactHandle(artifact_id=other.artifact_id, name="holdout"),
            "metrics": ArtifactHandle(
                artifact_id=fake_metrics.artifact_id, name="metrics"
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


def test_reproducible_rerun_tolerance(tmp_path, monkeypatch):
    _force_hard(monkeypatch)
    arts = LocalArtifactStore(tmp_path)
    check = CodeCheck(
        kind="reproducible_rerun",
        argv=["python3", "-c", "print(1.0)"],
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
    assert cr.effect and cr.effect.get("trusted") is True
    assert cr.effect.get("net_isolation") == "hard"
    assert cr.effect.get("fs_isolation") == "hard"


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
async def test_sandbox_infra_failure_still_finalizes(tmp_path, clock, runtime_cls):
    """Sandbox timeout → SANDBOX_INFRA; StudyFinalized invariant holds."""
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
    sid = "st_sbx_infra"
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
    await rt.start_attempt("REPORT")
    await rt.cancel_task("REPORT", reason="cancelled")
    aid = await rt.start_attempt("E1")
    result = await ex.record(
        aid,
        AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS),
    )
    assert result.outcome is Outcome.FAILED
    assert result.reason == TaskReason.SANDBOX_INFRA.value
    for _ in range(8):
        if getattr(rt, "_finalized", False):
            break
        await rt.tick()
        clock.advance(timedelta(seconds=5))
    view = rt.view()
    assert view.phase == "closed"
    assert getattr(rt, "_finalized", False)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_sandbox_infra_report_contains_reason_and_soft_note(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """MJ4: finalize without cancelling REPORT; report has infra reason + soft note."""
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
    # Complete REPORT successfully — do not cancel it (MJ4).
    await record_success(rt, await rt.start_attempt("REPORT"), arts)
    for _ in range(8):
        if getattr(rt, "_finalized", False):
            break
        await rt.tick()
        clock.advance(timedelta(seconds=5))
    assert getattr(rt, "_finalized", False)
    # Locate StudyFinalized + report.md text.
    events = await rt.store.read_all()
    fin = next(e for e in events if e.type == "StudyFinalized")
    report_id = fin.payload.get("report_artifact_id")
    assert report_id
    report_text = arts.read(report_id).decode()
    assert "sandbox_infra" in report_text or TaskReason.SANDBOX_INFRA.value in (
        fin.payload.get("contributing_reasons") or []
    )
    assert "sandbox_timeout" in report_text
    assert "blocklist is bypassable" in report_text or "network may be open" in report_text
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_exec_runs_production_path_trusted(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """Production executor path: hard isolation → put_sandbox → trusted evidence."""
    _force_hard(monkeypatch)
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
    assert sandbox_arts, "production path must call put_sandbox"
    assert all(a.net_isolation == "hard" for a in sandbox_arts)
    assert all(a.fs_isolation == "hard" for a in sandbox_arts)
    await close_runtime(rt)


def test_streaming_output_cap_kills(monkeypatch):
    """MJ1: overflow during incremental read → OUTPUT_EXCEEDED (not post-truncate)."""
    _force_soft(monkeypatch)
    r = run_sandbox(
        ["python3", "-c", "print('x'*100000)"],
        limits=SandboxLimits(timeout_s=5, memory_mb=64, max_output_bytes=1000),
    )
    assert r.status is SandboxStatus.OUTPUT_EXCEEDED
    assert len(r.stdout) <= 1000
