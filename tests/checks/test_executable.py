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
from tests.helpers import attach_executor, close_runtime, open_runtime


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


def test_sandbox_ok_exit_zero():
    r = run_sandbox(["/bin/true"], limits=SandboxLimits(timeout_s=5, memory_mb=64))
    assert r.status is SandboxStatus.OK
    assert r.exit_code == 0


def test_sandbox_timeout_is_infra_status():
    r = run_sandbox(
        ["python3", "-c", "import time; time.sleep(30)"],
        limits=SandboxLimits(timeout_s=0.3, memory_mb=64, max_output_bytes=10_000),
    )
    assert r.status is SandboxStatus.TIMEOUT
    assert r.reason == "sandbox_timeout"


def test_sandbox_no_network_by_default():
    # Connecting outward should fail inside the netns (not hang).
    r = run_sandbox(
        [
            "python3",
            "-c",
            "import socket; s=socket.socket(); s.settimeout(1); "
            "s.connect(('1.1.1.1', 80)); print('open')",
        ],
        limits=SandboxLimits(timeout_s=5, memory_mb=64, allow_network=False),
    )
    assert r.status in {
        SandboxStatus.NONZERO,
        SandboxStatus.CRASH,
        SandboxStatus.TIMEOUT,
        SandboxStatus.OK,  # rare if connect somehow succeeds locally
    }
    if r.status is SandboxStatus.OK:
        assert b"open" not in r.stdout


def test_exec_runs_exit_code(tmp_path):
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


def test_tests_pass_junit(tmp_path):
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
        check_id="tp#0",
        out=out,
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr.status is ReqStatus.MET
    assert any(
        arts.get_meta(e.artifact_id) and arts.get_meta(e.artifact_id).producer == "sandbox"
        for e in cr.evidence
    )

    bad = arts.put(
        b"""<?xml version="1.0"?><testsuite tests="1" failures="1" errors="0"/>""",
        make_tool_ref("st/E1#1:junit2"),
        name="junit2.xml",
        media_type="application/xml",
    )
    out2 = AgentTaskOutput(
        outputs={"junit": ArtifactHandle(artifact_id=bad.artifact_id, name="junit")}
    )
    cr2 = evaluate_code_check(
        check,
        template_id="tp",
        check_id="tp#1",
        out=out2,
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="st/E1#1",
    )
    assert cr2.status is ReqStatus.FAILED


def test_holdout_digest_frozen(tmp_path):
    arts = LocalArtifactStore(tmp_path)
    holdout = arts.put(
        b"holdout-bytes",
        make_tool_ref("st/E1#1:holdout"),
        name="holdout.bin",
        media_type="application/octet-stream",
    )
    metrics = arts.put(
        json.dumps({"score": 0.9}).encode(),
        make_tool_ref("st/E1#1:metrics"),
        name="metrics.json",
        media_type="application/json",
    )
    out = AgentTaskOutput(
        outputs={
            "holdout": ArtifactHandle(artifact_id=holdout.artifact_id, name="holdout"),
            "metrics": ArtifactHandle(artifact_id=metrics.artifact_id, name="metrics"),
        }
    )
    check = CodeCheck(
        kind="metric_on_holdout",
        output="metrics",
        holdout_output="holdout",
        holdout_digest=holdout.sha256,
        metric="score",
        pointer="/score",
        op=">=",
        target=0.5,
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
            "metrics": ArtifactHandle(artifact_id=metrics.artifact_id, name="metrics"),
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


def test_reproducible_rerun_tolerance(tmp_path):
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
    """Sandbox timeout → infra FAILED; StudyFinalized invariant holds."""
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
    ex, _arts = attach_executor(rt, tmp_path)
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
    # accept_plan marks PLAN DONE; cancel REPORT so all_done can finalize.
    await rt.start_attempt("REPORT")
    await rt.cancel_task("REPORT", reason="cancelled")
    aid = await rt.start_attempt("E1")
    result = await ex.record(
        aid,
        AgentTaskOutput(outputs={}, claim=Outcome.SUCCESS),
    )
    assert result.outcome is Outcome.FAILED
    assert result.reason == TaskReason.FAILED.value
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
async def test_exec_runs_production_path_trusted(tmp_path, clock, runtime_cls):
    """Production executor path: exec_runs → put_sandbox → trusted evidence."""
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
    await close_runtime(rt)
