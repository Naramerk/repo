"""M1 PR20: proposed_check catalog meta-template (DESIGN_M1 §16)."""

from __future__ import annotations

import json
import math
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from coscientist.checks.catalog import minimal_test_catalog
from coscientist.checks.evaluate import evaluate_task_checks
from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.claims import RuntimeClaim
from coscientist.core.clock import FakeClock
from coscientist.core.domain import (
    PROPOSED_CHECK_TEMPLATE_ID,
    AgentTaskOutput,
    ArtifactHandle,
    CheckProposal,
    CodeCheck,
    HitlPolicy,
    QuestionSpec,
    RejectedTemplateNote,
    ReqStatus,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.errors import AttemptClosedError, StudyClosedError
from coscientist.core.events import (
    STUDY_CREATED_SCHEMA_VERSION,
    STUDY_FINALIZED_SCHEMA_VERSION,
    EventEnvelope,
    make_idem,
)
from coscientist.core.finalize import finalize
from coscientist.core.proposed_check import (
    approve_check,
    compute_proposal_digest,
    fold_proposed_check_event,
    propose_check,
    reject_check,
    requirement_ref_for_proposal,
    schedule_proposed_check_review,
    validate_proposal_content,
)
from coscientist.core.reasons import TaskReason
from coscientist.core.rubric_types import Rubric, RubricItem
from coscientist.core.tool_loader import ensure_core_tools_ready
from coscientist.core.tool_registry import get_registered
from coscientist.core.upcasters import UPCASTERS, register_upcaster, upcast
from coscientist.core.verdict import claim_is_severe, exclusion_reason, hypothesis_verdict
from coscientist.tools.contract import CallCtx
from coscientist.tools.propose_check_tool import ProposeCheckTool
from tests.helpers import (
    attach_executor,
    close_runtime,
    open_runtime,
    open_runtime_experimental,
)


def _assert_one_finalized(events: list) -> object:
    """Exactly one StudyFinalized; report_artifact_id XOR minimal_report; conclusion_ids."""
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1
    fin = fins[0]
    has_art = fin.payload.get("report_artifact_id") is not None
    has_min = fin.payload.get("minimal_report") is not None
    assert has_art ^ has_min, "report_artifact_id XOR minimal_report"
    cids = fin.payload.get("conclusion_ids")
    assert isinstance(cids, list) and len(cids) > 0, "conclusion_ids must be non-empty"
    return fin


def _rejected() -> list[RejectedTemplateNote]:
    return [
        RejectedTemplateNote(
            template_id="value_match",
            reason="fixed target 225 does not match open research metric",
        ),
        RejectedTemplateNote(
            template_id="artifact_present",
            reason="existence alone does not measure the claimed effect",
        ),
    ]


def _value_check(*, target: int = 42) -> CodeCheck:
    return CodeCheck(
        kind="value_matches",
        output="main",
        pointer="/count",
        target=target,
    )


def _task_id(rt, key: str) -> str:
    for t in rt.tasks.values():
        if t.spec.task_key == key:
            return t.task_id
    raise AssertionError(f"no task with key {key!r}")


def _exp_spec(**kwargs) -> TaskSpec:
    return TaskSpec(
        task_key=kwargs.get("task_key", "EXP"),
        title=kwargs.get("title", "e"),
        goal=kwargs.get("goal", "g"),
        route=kwargs.get("route", "experiment"),
        depends_on=kwargs.get("depends_on", ["PLAN"]),
        addresses=kwargs.get("addresses", []),
        criteria=kwargs.get(
            "criteria", [RequirementRef(template_id="artifact_present")]
        ),
        expected_outputs=kwargs.get("expected_outputs", []),
    )


async def _create(rt, request: str = "pr20", **kwargs):
    await rt.create_study(
        profile="t",
        request=request,
        catalog_version="0.1.0",
        **kwargs,
    )


async def _boot_plan(rt, tasks, *, questions=None):
    """Cancel SPEC/HYP/LIT then accept_plan; return SPEC proposer_aid for prereg."""
    proposer_aid = await rt.start_attempt("SPEC")
    assert proposer_aid
    await rt.cancel_task("SPEC", reason="cancelled")
    for key in ("HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(tasks, questions=questions or [])
    return proposer_aid


async def _finish_report(rt, tmp_path: Path) -> None:
    aid = await rt.start_attempt("REPORT")
    if aid is None:
        await rt.ensure_lease(ttl_s=10_000)
        await rt.tick()
        return
    ex, arts = attach_executor(rt, tmp_path)
    art = arts.put(
        b"# report\n",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="text/markdown",
    )
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="report",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
        ),
    )
    await rt.ensure_lease(ttl_s=10_000)
    await rt.tick()


def _set_proposed_criteria(rt, task_id: str, pid: str, *extra: RequirementRef) -> None:
    task = rt.tasks[task_id]
    criteria = [requirement_ref_for_proposal(pid), *extra]
    rt.tasks[task_id] = task.model_copy(
        update={"spec": task.spec.model_copy(update={"criteria": criteria})}
    )


@pytest.mark.asyncio
async def test_propose_preregisters_digest_before_result(tmp_path: Path) -> None:
    """CheckProposed is written with a fixed digest before any evaluating attempt."""
    rt = await open_runtime(tmp_path, "st_pr20_pre")
    try:
        await _create(rt, "propose digest")
        proposer_aid = await _boot_plan(
            rt,
            [
                _exp_spec(
                    addresses=["q1"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                )
            ],
            questions=[QuestionSpec(id="q1", text="Q?")],
        )
        exp_tid = _task_id(rt, "EXP")
        pid = await propose_check(
            rt,
            target="q1",
            role="prediction",
            measures="count equals 42",
            justification="catalog templates assume fixed ontology metrics",
            rejected_templates=_rejected(),
            check=_value_check(target=42),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid
        row = rt._check_proposals[pid]
        digest = row["proposal_digest"]
        assert digest == compute_proposal_digest(row)
        assert row["status"] == "proposed"
        assert not any(
            (isinstance(r, dict) and r.get("proposal_id") == pid)
            for r in (rt.results or {}).values()
        )
        events = await rt.store.read_all()
        proposed = [e for e in events if e.type == "CheckProposed"]
        assert len(proposed) == 1
        assert proposed[0].payload["proposal_digest"] == digest
        assert proposed[0].schema_version == 1
        created = [e for e in events if e.type == "StudyCreated"][0]
        assert created.schema_version == STUDY_CREATED_SCHEMA_VERSION
        assert created.schema_version == 12
        assert created.payload.get("max_proposals") == 3
        # Evaluating attempt starts only after CheckProposed.
        aid = await rt.start_attempt("EXP")
        assert aid
        assert rt.attempts[aid]["_seq"] > proposed[0].seq
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_unapproved_check_excluded_from_verdict(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_pr20_unapp")
    try:
        await _create(rt, "unapproved")
        proposer_aid = await _boot_plan(
            rt,
            [
                _exp_spec(
                    addresses=["q1"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                )
            ],
            questions=[QuestionSpec(id="q1", text="Q?")],
        )
        exp_tid = _task_id(rt, "EXP")
        pid = await propose_check(
            rt,
            target="q1",
            role="prediction",
            measures="count",
            justification="need custom threshold",
            rejected_templates=_rejected(),
            check=_value_check(target=42),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid
        _set_proposed_criteria(rt, exp_tid, pid)
        aid = await rt.start_attempt("EXP")
        assert aid
        ex, arts = attach_executor(rt, tmp_path)
        content = json.dumps({"count": 42}).encode()
        art = arts.put(
            content, make_tool_ref(f"{aid}:1"), name="main", media_type="application/json"
        )
        result = await ex.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={
                    "main": ArtifactHandle(artifact_id=art.artifact_id, name="main")
                },
                values={"count": 42},
                claims=[{"target": "q1", "target_kind": "question"}],
            ),
        )
        assert any(c.status == ReqStatus.PENDING for c in result.check_results)
        assert any(
            (c.effect or {}).get("exclusion_reason") == "unapproved_check"
            for c in result.check_results
        )
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_approved_executable_counts_like_catalog(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_pr20_ok")
    try:
        await _create(rt, "approved exec")
        proposer_aid = await _boot_plan(
            rt,
            [
                _exp_spec(
                    addresses=["q1"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                )
            ],
            questions=[QuestionSpec(id="q1", text="Q?")],
        )
        exp_tid = _task_id(rt, "EXP")
        pid = await propose_check(
            rt,
            target="q1",
            role="prediction",
            measures="count==42",
            justification="custom",
            rejected_templates=_rejected(),
            check=_value_check(target=42),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid
        await approve_check(rt, pid, source="api", delegated=False)
        assert rt._check_proposals[pid]["status"] == "approved"
        _set_proposed_criteria(
            rt, exp_tid, pid, RequirementRef(template_id="artifact_present")
        )
        aid = await rt.start_attempt("EXP")
        assert aid
        ex, arts = attach_executor(rt, tmp_path)
        content = json.dumps({"count": 42}).encode()
        art = arts.put(
            content, make_tool_ref(f"{aid}:1"), name="main", media_type="application/json"
        )
        await ex.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={
                    "main": ArtifactHandle(artifact_id=art.artifact_id, name="main")
                },
                values={"count": 42},
                claims=[{"target": "q1", "target_kind": "question"}],
            ),
        )
        events = await rt.store.read_all()
        recorded = [e for e in events if e.type == "ResultRecorded"]
        assert recorded
        checks = recorded[-1].payload.get("check_results") or []
        proposed = [c for c in checks if c.get("template_id") == PROPOSED_CHECK_TEMPLATE_ID]
        assert proposed and proposed[0]["status"] == ReqStatus.MET.value
        assert proposed[0].get("effect", {}).get("proposed_check") is True
        assert proposed[0].get("effect", {}).get("delegated") is False
    finally:
        await close_runtime(rt)


def test_approved_rubric_never_severe() -> None:
    claim = RuntimeClaim(
        accepted=True,
        frozen=True,
        provenance="real",
        validity_ok=True,
        claim_outcome="consistent",
        criterion_status="met",
        evidence_kind="computational",
        uses_sesoi=True,
        used_sesoi=True,
        sesoi=0.5,
        effect={
            "proposed_check": True,
            "qualitative": True,
            "never_severe": True,
            "countable": True,
            "trusted": True,
            "core_evaluated": True,
            "est": 1.0,
        },
    )
    assert claim_is_severe(claim) is False


def test_delegated_does_not_tip_catalog_tie() -> None:
    catalog_refute = RuntimeClaim(
        accepted=True,
        frozen=True,
        provenance="real",
        validity_ok=True,
        claim_outcome="inconsistent",
        criterion_status="not_met",
        evidence_kind="computational",
        independence_key="exp_a",
        effect={"countable": True, "core_evaluated": True, "trusted": True, "est": 0.1},
    )
    catalog_support = RuntimeClaim(
        accepted=True,
        frozen=True,
        provenance="real",
        validity_ok=True,
        claim_outcome="consistent",
        criterion_status="met",
        evidence_kind="computational",
        independence_key="exp_b",
        uses_sesoi=True,
        used_sesoi=True,
        sesoi=0.1,
        intent="refute",
        effect={
            "countable": True,
            "core_evaluated": True,
            "trusted": True,
            "est": 1.0,
            "ci_low": 0.5,
            "ci_high": 1.5,
        },
    )
    delegated_extra = RuntimeClaim(
        accepted=True,
        frozen=True,
        provenance="real",
        validity_ok=True,
        claim_outcome="consistent",
        criterion_status="met",
        evidence_kind="computational",
        independence_key="exp_c",
        uses_sesoi=True,
        used_sesoi=True,
        sesoi=0.1,
        intent="refute",
        effect={
            "proposed_check": True,
            "delegated": True,
            "countable": True,
            "core_evaluated": True,
            "trusted": True,
            "est": 2.0,
            "ci_low": 1.0,
            "ci_high": 3.0,
        },
    )
    v_cat, _, _ = hypothesis_verdict(
        [catalog_refute, catalog_support],
        refutations_needed=1,
        supported_needed=2,
        refutation_priority=True,
    )
    v_all, _, _ = hypothesis_verdict(
        [catalog_refute, catalog_support, delegated_extra],
        refutations_needed=1,
        supported_needed=2,
        refutation_priority=True,
    )
    assert v_all == v_cat


def test_delegated_zero_zero_catalog_not_supported() -> None:
    """0:0 catalog tie + delegated severe support must not become supported."""

    def _delegated(key: str) -> RuntimeClaim:
        return RuntimeClaim(
            accepted=True,
            frozen=True,
            provenance="real",
            validity_ok=True,
            claim_outcome="consistent",
            criterion_status="met",
            evidence_kind="computational",
            independence_key=key,
            uses_sesoi=True,
            used_sesoi=True,
            sesoi=0.1,
            intent="refute",
            effect={
                "proposed_check": True,
                "delegated": True,
                "countable": True,
                "core_evaluated": True,
                "trusted": True,
                "est": 2.0,
                "ci_low": 1.0,
                "ci_high": 3.0,
            },
        )

    v, _, _ = hypothesis_verdict(
        [_delegated("d1"), _delegated("d2")],
        refutations_needed=1,
        supported_needed=2,
        refutation_priority=True,
    )
    assert v != "supported"


@pytest.mark.asyncio
async def test_proposal_quota_closes_task(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_pr20_quota")
    try:
        await _create(rt, "quota", max_proposals=1)
        assert rt.study is not None
        assert rt.study.max_proposals == 1
        proposer_aid = await _boot_plan(
            rt,
            [_exp_spec()],
        )
        exp_tid = _task_id(rt, "EXP")
        pid1 = await propose_check(
            rt,
            target="study",
            role="validity",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid1
        pid2 = await propose_check(
            rt,
            target="study",
            role="validity",
            measures="m2",
            justification="j2",
            rejected_templates=_rejected(),
            check=_value_check(target=2),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid2 is None
        task = rt.tasks[exp_tid]
        assert task.reason == TaskReason.PROPOSAL_QUOTA
        await _finish_report(rt, tmp_path)
        await rt.ensure_lease(ttl_s=10_000)
        await rt.tick()
        if not getattr(rt, "_finalized", False):
            await finalize(rt, trigger="all_done")
        assert rt._finalized
        events = await rt.store.read_all()
        _assert_one_finalized(events)
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_max_proposals_zero_immediate_quota(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_pr20_q0")
    try:
        await _create(rt, "quota0", max_proposals=0)
        proposer_aid = await _boot_plan(rt, [_exp_spec()])
        exp_tid = _task_id(rt, "EXP")
        pid = await propose_check(
            rt,
            target="study",
            role="validity",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid is None
        assert rt.tasks[exp_tid].reason == TaskReason.PROPOSAL_QUOTA
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_quota_close_late_record_attempt_closed(tmp_path: Path) -> None:
    """Quota close then late executor.record → AttemptClosedError / LateWriteRejected."""
    rt = await open_runtime(tmp_path, "st_pr20_quota_late")
    try:
        await _create(rt, "quota late", max_proposals=1)
        proposer_aid = await _boot_plan(rt, [_exp_spec()])
        exp_tid = _task_id(rt, "EXP")
        aid = await rt.start_attempt("EXP")
        assert aid
        pid1 = await propose_check(
            rt,
            target="study",
            role="validity",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=exp_tid,
            attempt_id=aid,
            open_hitl=False,
        )
        assert pid1
        pid2 = await propose_check(
            rt,
            target="study",
            role="validity",
            measures="m2",
            justification="j2",
            rejected_templates=_rejected(),
            check=_value_check(target=2),
            task_id=exp_tid,
            attempt_id=aid,
            open_hitl=False,
        )
        assert pid2 is None
        assert rt.attempts[aid].get("closed")
        ex, arts = attach_executor(rt, tmp_path)
        art = arts.put(
            b"{}", make_tool_ref(f"{aid}:1"), name="main", media_type="application/json"
        )
        with pytest.raises(AttemptClosedError):
            await ex.record(
                aid,
                AgentTaskOutput(
                    summary="late",
                    outputs={
                        "main": ArtifactHandle(artifact_id=art.artifact_id, name="main")
                    },
                ),
            )
        events = await rt.store.read_all()
        assert any(e.type == "LateWriteRejected" for e in events)
        dones = [
            e
            for e in events
            if e.type == "TaskDone" and e.entity_key == exp_tid
        ]
        assert len(dones) == 1
        assert dones[0].payload.get("reason") == TaskReason.PROPOSAL_QUOTA.value
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_reject_and_timeout_still_finalize(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_pr20_rej")
    try:
        await _create(rt, "reject")
        proposer_aid = await _boot_plan(rt, [_exp_spec()])
        exp_tid = _task_id(rt, "EXP")
        pid = await propose_check(
            rt,
            target="study",
            role="validity",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid
        await reject_check(rt, pid, source="timeout", reason="human_timeout")
        assert rt._check_proposals[pid]["status"] == "rejected"
        aid = await rt.start_attempt("EXP")
        assert aid
        ex, arts = attach_executor(rt, tmp_path)
        art = arts.put(
            b"{}", make_tool_ref(f"{aid}:1"), name="main", media_type="application/json"
        )
        await ex.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={
                    "main": ArtifactHandle(artifact_id=art.artifact_id, name="main")
                },
            ),
        )
        await _finish_report(rt, tmp_path)
        await rt.ensure_lease(ttl_s=10_000)
        await rt.tick()
        assert rt._finalized
        events = await rt.store.read_all()
        fin = _assert_one_finalized(events)
        assert fin.schema_version == STUDY_FINALIZED_SCHEMA_VERSION
        assert "custom_checks" in fin.payload
        assert fin.payload["custom_checks"][0]["status"] == "rejected"
        assert fin.payload["custom_checks"][0]["catalog"] is False
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_post_delegate_timeout_equiv_reject(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    rt = await open_runtime(tmp_path, "st_pr20_del_to", clock=clock)
    try:
        await _create(rt, "delegate timeout")
        proposer_aid = await _boot_plan(rt, [_exp_spec()])
        exp_tid = _task_id(rt, "EXP")
        pid = await propose_check(
            rt,
            target="study",
            role="prediction",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid
        await schedule_proposed_check_review(rt, pid, timeout_s=5)
        assert rt._proposed_check_reviews[pid]["status"] == "pending"
        clock.advance(timedelta(seconds=10))
        await rt.ensure_lease(ttl_s=10_000)
        await rt.tick()
        assert rt._check_proposals[pid]["status"] == "rejected"
        assert rt._check_proposals[pid].get("reject_reason") == "human_timeout"
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_no_append_after_study_finalized(tmp_path: Path) -> None:
    """CheckProposed / CheckApproved / CheckRejected rejected after StudyFinalized."""
    rt = await open_runtime(tmp_path, "st_pr20_closed")
    try:
        await _create(rt, "closed")
        proposer_aid = await _boot_plan(rt, [_exp_spec()])
        exp_tid = _task_id(rt, "EXP")
        pid = await propose_check(
            rt,
            target="study",
            role="validity",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid
        aid = await rt.start_attempt("EXP")
        assert aid
        ex, arts = attach_executor(rt, tmp_path)
        art = arts.put(
            b"{}", make_tool_ref(f"{aid}:1"), name="main", media_type="application/json"
        )
        await ex.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={
                    "main": ArtifactHandle(artifact_id=art.artifact_id, name="main")
                },
            ),
        )
        await _finish_report(rt, tmp_path)
        await rt.ensure_lease(ttl_s=10_000)
        await rt.tick()
        if not getattr(rt, "_finalized", False):
            await finalize(rt, trigger="all_done")
        assert rt._finalized
        events_before = await rt.store.read_all()
        n_before = len(events_before)
        _assert_one_finalized(events_before)
        with pytest.raises(StudyClosedError):
            await propose_check(
                rt,
                target="study",
                role="validity",
                measures="late",
                justification="j",
                rejected_templates=_rejected(),
                check=_value_check(target=9),
                task_id=exp_tid,
                attempt_id=proposer_aid,
                open_hitl=False,
            )
        with pytest.raises(StudyClosedError):
            await approve_check(rt, pid, source="api")
        with pytest.raises(StudyClosedError):
            await reject_check(rt, pid, source="timeout", reason="too_late")
        events_after = await rt.store.read_all()
        assert len(events_after) == n_before
        assert len([e for e in events_after if e.type == "StudyFinalized"]) == 1
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_kill_reopen_restores_proposals(tmp_path: Path) -> None:
    root = tmp_path / "data"
    sid = "st_pr20_reopen"
    rt = await open_runtime(root, sid)
    try:
        await _create(rt, "reopen")
        proposer_aid = await _boot_plan(rt, [_exp_spec()])
        exp_tid = _task_id(rt, "EXP")
        pid = await propose_check(
            rt,
            target="study",
            role="prediction",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=7),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid
        digest = rt._check_proposals[pid]["proposal_digest"]
    finally:
        await close_runtime(rt)

    rt2 = await open_runtime(root, sid)
    try:
        assert pid in rt2._check_proposals
        assert rt2._check_proposals[pid]["proposal_digest"] == digest
        assert rt2._check_proposals[pid]["status"] == "proposed"
        await approve_check(rt2, pid, source="api")
        assert rt2._check_proposals[pid]["status"] == "approved"
    finally:
        await close_runtime(rt2)


@pytest.mark.asyncio
async def test_restart_open_hitl_and_delegated_review_live_eq_replay(
    tmp_path: Path,
) -> None:
    """open HITL + ReviewScheduled survive restart; live status == replay."""
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    root = tmp_path / "data"
    sid = "st_pr20_hitl_replay"
    rt = await open_runtime_experimental(root, sid, clock=clock)
    try:
        await _create(rt, "hitl replay")
        proposer_aid = await _boot_plan(rt, [_exp_spec()])
        exp_tid = _task_id(rt, "EXP")
        pid = await propose_check(
            rt,
            target="study",
            role="prediction",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=True,
            timeout_s=120,
        )
        assert pid
        await schedule_proposed_check_review(rt, pid, timeout_s=60)
        live_status = rt._check_proposals[pid]["status"]
        live_review = dict(rt._proposed_check_reviews[pid])
        assert live_status == "proposed"
        assert live_review.get("status") == "pending"
        assert any(
            e.type == "HitlOpened" and e.payload.get("payload_ref") == pid
            for e in await rt.store.read_all()
        )
    finally:
        await close_runtime(rt)

    rt2 = await open_runtime_experimental(root, sid, clock=clock)
    try:
        assert rt2._check_proposals[pid]["status"] == live_status
        assert rt2._proposed_check_reviews[pid]["status"] == live_review["status"]
        assert rt2._proposed_check_reviews[pid].get("kind") == "proposed_check_review"
    finally:
        await close_runtime(rt2)


@pytest.mark.asyncio
async def test_delegated_reviewer_adversarial(tmp_path: Path) -> None:
    """Reviewer must differ / be reviewer role / live / different task."""
    rt = await open_runtime(tmp_path, "st_pr20_del")
    try:
        await _create(rt, "delegated")
        proposer_aid = await _boot_plan(
            rt,
            [
                _exp_spec(task_key="EXP"),
                _exp_spec(task_key="REV_AGENT", title="agent"),
                _exp_spec(task_key="REV_CLOSED", title="closed"),
                _exp_spec(task_key="REV_OK", title="ok"),
            ],
        )
        exp_tid = _task_id(rt, "EXP")
        pid = await propose_check(
            rt,
            target="study",
            role="prediction",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid
        await schedule_proposed_check_review(rt, pid, timeout_s=60)

        # Same attempt id as author (even if that attempt is already closed).
        with pytest.raises(ValueError, match="differ"):
            await approve_check(
                rt,
                pid,
                source="reviewer",
                delegated=True,
                reviewer_attempt_id=proposer_aid,
            )

        agent_rev = await rt.start_attempt("REV_AGENT", attempt_role="agent")
        assert agent_rev
        with pytest.raises(ValueError, match="attempt_role=reviewer"):
            await approve_check(
                rt,
                pid,
                source="reviewer",
                delegated=True,
                reviewer_attempt_id=agent_rev,
            )

        same_task = await rt.start_attempt("EXP", attempt_role="reviewer")
        assert same_task
        with pytest.raises(ValueError, match="different task"):
            await approve_check(
                rt,
                pid,
                source="reviewer",
                delegated=True,
                reviewer_attempt_id=same_task,
            )

        with pytest.raises(ValueError, match="unknown reviewer_attempt_id"):
            await approve_check(
                rt,
                pid,
                source="reviewer",
                delegated=True,
                reviewer_attempt_id="bogus-reviewer-aid",
            )

        closed = await rt.start_attempt("REV_CLOSED", attempt_role="reviewer")
        assert closed
        await rt.cancel_task("REV_CLOSED", reason="cancelled")
        with pytest.raises(AttemptClosedError):
            await approve_check(
                rt,
                pid,
                source="reviewer",
                delegated=True,
                reviewer_attempt_id=closed,
            )

        ok = await rt.start_attempt("REV_OK", attempt_role="reviewer")
        assert ok
        await approve_check(
            rt,
            pid,
            source="reviewer",
            delegated=True,
            reviewer_attempt_id=ok,
        )
        assert rt._check_proposals[pid]["delegated"] is True
        assert rt._check_proposals[pid]["status"] == "approved"
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_approve_source_human_and_reviewer_guards(tmp_path: Path) -> None:
    """by/human not settable; human only via hitl/api; reviewer needs delegated."""
    rt = await open_runtime(tmp_path, "st_pr20_src")
    try:
        await _create(rt, "sources")
        proposer_aid = await _boot_plan(rt, [_exp_spec()])
        exp_tid = _task_id(rt, "EXP")
        pid = await propose_check(
            rt,
            target="study",
            role="prediction",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid
        with pytest.raises(TypeError):
            await approve_check(rt, pid, by="human")  # type: ignore[call-arg]
        with pytest.raises(ValueError, match="invalid approval source"):
            await approve_check(rt, pid, source="human")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="delegated=True"):
            await approve_check(rt, pid, source="reviewer", delegated=False)
        with pytest.raises(ValueError, match="reviewer_attempt_id|unknown"):
            await approve_check(
                rt,
                pid,
                source="reviewer",
                delegated=True,
                reviewer_attempt_id="not-a-real-attempt",
            )
        await approve_check(rt, pid, source="api")
        assert rt._check_proposals[pid]["status"] == "approved"
        events = await rt.store.read_all()
        approved = [e for e in events if e.type == "CheckApproved"]
        assert len(approved) == 1
        assert approved[0].idem_key == make_idem("CheckApproved", pid)
        assert approved[0].idem_key == f"CheckApproved:{pid}"
    finally:
        await close_runtime(rt)


def test_check_approved_idem_key_no_payload_hash() -> None:
    pid = "prop_abc"
    key = make_idem("CheckApproved", pid)
    assert key == f"CheckApproved:{pid}"
    assert key.count(":") == 1


@pytest.mark.asyncio
async def test_prereg_after_attempt_fails_eval(tmp_path: Path) -> None:
    """Adversarial: propose AFTER evaluating attempt start → prereg_after_attempt."""
    rt = await open_runtime(tmp_path, "st_pr20_prereg_late")
    try:
        await _create(rt, "prereg late")
        await _boot_plan(
            rt,
            [
                _exp_spec(
                    addresses=["q1"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                )
            ],
            questions=[QuestionSpec(id="q1", text="Q?")],
        )
        exp_tid = _task_id(rt, "EXP")
        aid = await rt.start_attempt("EXP")
        assert aid
        pid = await propose_check(
            rt,
            target="q1",
            role="prediction",
            measures="count",
            justification="too late",
            rejected_templates=_rejected(),
            check=_value_check(target=42),
            task_id=exp_tid,
            attempt_id=aid,
            open_hitl=False,
        )
        assert pid
        await approve_check(rt, pid, source="api")
        _set_proposed_criteria(rt, exp_tid, pid)
        ex, arts = attach_executor(rt, tmp_path)
        content = json.dumps({"count": 42}).encode()
        art = arts.put(
            content, make_tool_ref(f"{aid}:1"), name="main", media_type="application/json"
        )
        await ex.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={
                    "main": ArtifactHandle(artifact_id=art.artifact_id, name="main")
                },
                values={"count": 42},
                claims=[{"target": "q1", "target_kind": "question"}],
            ),
        )
        events = await rt.store.read_all()
        recorded = [e for e in events if e.type == "ResultRecorded"]
        assert recorded
        checks = recorded[-1].payload.get("check_results") or []
        proposed = [
            c for c in checks if c.get("template_id") == PROPOSED_CHECK_TEMPLATE_ID
        ]
        assert proposed
        assert proposed[0]["status"] == ReqStatus.FAILED.value
        assert "prereg_after_attempt" in (proposed[0].get("detail") or "")
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_approval_cannot_bind_another_task_id(tmp_path: Path) -> None:
    """One approval cannot feed a different task_id toward counting."""
    rt = await open_runtime(tmp_path, "st_pr20_bind")
    try:
        await _create(rt, "bind")
        proposer_aid = await _boot_plan(
            rt,
            [
                _exp_spec(task_key="EXP", addresses=["q1"]),
                _exp_spec(task_key="EXP2", addresses=["q1"], title="other"),
            ],
            questions=[QuestionSpec(id="q1", text="Q?")],
        )
        exp_tid = _task_id(rt, "EXP")
        exp2_tid = _task_id(rt, "EXP2")
        pid = await propose_check(
            rt,
            target="q1",
            role="prediction",
            measures="count",
            justification="bound to EXP",
            rejected_templates=_rejected(),
            check=_value_check(target=42),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid
        await approve_check(rt, pid, source="api")
        _set_proposed_criteria(rt, exp2_tid, pid)
        aid = await rt.start_attempt("EXP2")
        assert aid
        ex, arts = attach_executor(rt, tmp_path)
        content = json.dumps({"count": 42}).encode()
        art = arts.put(
            content, make_tool_ref(f"{aid}:1"), name="main", media_type="application/json"
        )
        await ex.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={
                    "main": ArtifactHandle(artifact_id=art.artifact_id, name="main")
                },
                values={"count": 42},
                claims=[{"target": "q1", "target_kind": "question"}],
            ),
        )
        events = await rt.store.read_all()
        recorded = [e for e in events if e.type == "ResultRecorded"]
        assert recorded
        checks = recorded[-1].payload.get("check_results") or []
        proposed = [
            c for c in checks if c.get("template_id") == PROPOSED_CHECK_TEMPLATE_ID
        ]
        assert proposed
        assert proposed[0]["status"] == ReqStatus.FAILED.value
        assert "task_mismatch" in (proposed[0].get("detail") or "")
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_cannot_propose_after_result_recorded(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_pr20_after_res")
    try:
        await _create(rt, "after result")
        proposer_aid = await _boot_plan(rt, [_exp_spec()])
        exp_tid = _task_id(rt, "EXP")
        aid = await rt.start_attempt("EXP")
        assert aid
        ex, arts = attach_executor(rt, tmp_path)
        art = arts.put(
            b"{}", make_tool_ref(f"{aid}:1"), name="main", media_type="application/json"
        )
        await ex.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={
                    "main": ArtifactHandle(artifact_id=art.artifact_id, name="main")
                },
            ),
        )
        with pytest.raises(ValueError, match="result already recorded"):
            await propose_check(
                rt,
                target="study",
                role="validity",
                measures="late",
                justification="j",
                rejected_templates=_rejected(),
                check=_value_check(target=1),
                task_id=exp_tid,
                attempt_id=proposer_aid,
                open_hitl=False,
            )
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_propose_check_tool_e2e_accept_plan_finalize(tmp_path: Path) -> None:
    """Loader already has propose_check; tool → accept_plan criterion → finalize."""
    ensure_core_tools_ready()
    assert get_registered("propose_check") is not None
    rt = await open_runtime(tmp_path, "st_pr20_tool")
    try:
        await _create(rt, "tool e2e")
        proposer_aid = await rt.start_attempt("SPEC")
        assert proposer_aid
        await rt.cancel_task("SPEC", reason="cancelled")
        for key in ("HYP", "LIT"):
            await rt.start_attempt(key)
            await rt.cancel_task(key, reason="cancelled")
        exp_tid = f"{rt.study.study_id}/EXP"
        tool = ProposeCheckTool()
        ctx = CallCtx(
            study_id=rt.study.study_id,
            task_key="SPEC",
            attempt_id=proposer_aid,
            hitl_policy=rt.study.hitl_policy,
            extra={"runtime": rt},
        )
        out = await tool.call(
            {
                "target": "q1",
                "role": "prediction",
                "measures": "count==42",
                "justification": "custom via agent tool",
                "rejected_templates": [n.model_dump(mode="json") for n in _rejected()],
                "task_id": exp_tid,
                "check": _value_check(target=42).model_dump(mode="json"),
                "open_hitl": False,
            },
            ctx,
        )
        pid = out.data["proposal_id"]
        assert pid
        ref = requirement_ref_for_proposal(pid)
        assert out.data["criterion"]["template_id"] == PROPOSED_CHECK_TEMPLATE_ID
        await rt.accept_plan(
            [
                _exp_spec(
                    addresses=["q1"],
                    criteria=[ref, RequirementRef(template_id="artifact_present")],
                )
            ],
            questions=[QuestionSpec(id="q1", text="Q?")],
        )
        await approve_check(rt, pid, source="api")
        aid = await rt.start_attempt("EXP")
        assert aid
        ex, arts = attach_executor(rt, tmp_path)
        content = json.dumps({"count": 42}).encode()
        art = arts.put(
            content, make_tool_ref(f"{aid}:1"), name="main", media_type="application/json"
        )
        await ex.record(
            aid,
            AgentTaskOutput(
                summary="ok",
                outputs={
                    "main": ArtifactHandle(artifact_id=art.artifact_id, name="main")
                },
                values={"count": 42},
                claims=[{"target": "q1", "target_kind": "question"}],
            ),
        )
        await _finish_report(rt, tmp_path)
        await rt.ensure_lease(ttl_s=10_000)
        await rt.tick()
        if not getattr(rt, "_finalized", False):
            await finalize(rt, trigger="all_done")
        assert rt._finalized
        events = await rt.store.read_all()
        _assert_one_finalized(events)
    finally:
        await close_runtime(rt)


def test_upcaster_studycreated_v10_bridge_then_v12_max_proposals() -> None:
    """Main v10 → #23 v11 (as_of/user_input_ids) → PR20 v12 (max_proposals)."""
    v, payload = upcast("StudyCreated", 10, {"study_id": "s", "max_loop_iter": 8})
    assert v == STUDY_CREATED_SCHEMA_VERSION == 12
    assert payload.get("max_proposals") == 3
    assert payload.get("as_of") is None
    assert payload.get("user_input_ids") == []
    assert payload.get("max_loop_iter") == 8
    v12, p12 = upcast(
        "StudyCreated", 11, {"study_id": "s", "as_of": None, "user_input_ids": []}
    )
    assert v12 == 12
    assert p12.get("max_proposals") == 3
    assert 10 in UPCASTERS["StudyCreated"] and 11 in UPCASTERS["StudyCreated"]


def test_upcaster_studycreated_v10_preserves_max_loop_iter() -> None:
    """v10→v11 bridge must not pop max_loop_iter (real cap on main / identical to #23)."""
    mid = UPCASTERS["StudyCreated"][10]({"study_id": "s", "max_loop_iter": 2})
    assert mid["max_loop_iter"] == 2
    assert "max_loop_iter" in mid
    assert mid.get("as_of") is None
    assert mid.get("user_input_ids") == []
    v, payload = upcast("StudyCreated", 10, {"study_id": "s", "max_loop_iter": 2})
    assert v == 12
    assert payload["max_loop_iter"] == 2
    assert payload.get("max_proposals") == 3


def test_upcaster_studycreated_v11_to_v12_preserves_max_proposals() -> None:
    """11→12: existing max_proposals=5 stays 5; missing → default 3."""
    v, p = upcast(
        "StudyCreated",
        11,
        {"study_id": "s", "as_of": None, "user_input_ids": [], "max_proposals": 5},
    )
    assert v == 12
    assert p["max_proposals"] == 5
    v2, p2 = upcast(
        "StudyCreated", 11, {"study_id": "s", "as_of": None, "user_input_ids": []}
    )
    assert v2 == 12
    assert p2["max_proposals"] == 3


@pytest.mark.asyncio
async def test_replay_studycreated_v10_max_loop_iter_preserved(tmp_path: Path) -> None:
    """Seeded StudyCreated v10 with max_loop_iter=2 reopens as study.max_loop_iter==2."""
    from coscientist.core.store import SqliteEventStore

    sid = "st_pr20_v10_loop"
    root = tmp_path / sid
    root.mkdir(parents=True)
    store = SqliteEventStore(sid, root / "events.db")
    await store.acquire_lease("seeder")
    now = datetime.now(timezone.utc)
    dl = now + timedelta(seconds=120)
    await store.append(
        EventEnvelope(
            study_id=sid,
            type="StudyCreated",
            entity_key=sid,
            idem_key=make_idem("StudyCreated", sid),
            payload={
                "study_id": sid,
                "profile": "research",
                "request": "preserve loop cap",
                "catalog_version": "0.1.0",
                "hitl_policy": HitlPolicy().model_dump(mode="json"),
                "deadline": dl.isoformat(),
                "created_at": now.isoformat(),
                "max_loop_iter": 2,
            },
            ts=now,
            schema_version=10,
        )
    )
    await store.release_lease("seeder", store.epoch)
    await store.close()

    rt = await open_runtime(tmp_path, sid)
    try:
        assert rt.study is not None
        assert rt.study.max_loop_iter == 2
        assert rt.study.max_proposals == 3
        assert rt.study.as_of is None
        assert rt.study.user_input_ids == []
        created = [e for e in await rt.store.read_all() if e.type == "StudyCreated"]
        assert len(created) == 1
        assert created[0].schema_version == STUDY_CREATED_SCHEMA_VERSION
        assert created[0].payload["max_loop_iter"] == 2
    finally:
        await close_runtime(rt)


def test_upcaster_studyfinalized_v3_to_v4_custom_checks() -> None:
    v, payload = upcast("StudyFinalized", 3, {"trigger": "all_done"})
    assert v == STUDY_FINALIZED_SCHEMA_VERSION == 4
    assert payload.get("custom_checks") == []
    assert payload.get("observations") == []


def test_register_upcaster_refuses_overwrite() -> None:
    with pytest.raises(ValueError, match="duplicate upcaster"):
        register_upcaster("StudyCreated", 9, lambda p: p)
    assert 9 in UPCASTERS["StudyCreated"]


def test_evaluate_proposed_missing_id_fail_closed(tmp_path: Path) -> None:
    catalog = minimal_test_catalog()
    arts = LocalArtifactStore(tmp_path / "arts")
    out = AgentTaskOutput(summary="x")
    spec = TaskSpec(
        task_key="EXP",
        title="t",
        goal="g",
        route="agent",
        criteria=[
            RequirementRef(template_id=PROPOSED_CHECK_TEMPLATE_ID, params={}),
        ],
    )
    results = evaluate_task_checks(catalog, spec, out, arts, proposals={})
    assert results[0].status == ReqStatus.FAILED
    assert "unapproved_check" in (results[0].detail or "")


def test_exclusion_reason_unapproved() -> None:
    c = RuntimeClaim(
        accepted=True,
        frozen=True,
        provenance="real",
        validity_ok=False,
        exclusion_reason="unapproved_check",
        claim_outcome="consistent",
        criterion_status="met",
        evidence_kind="computational",
    )
    assert c.counts() is False
    assert exclusion_reason(c) == "unapproved_check"


def test_proposal_requires_rejected_templates() -> None:
    with pytest.raises(ValueError, match="rejected_templates"):
        validate_proposal_content(
            CheckProposal(
                proposal_id="p",
                target="q",
                role="prediction",
                measures="m",
                check=_value_check(target=1),
                justification="because",
                rejected_templates=[],
            )
        )


def test_proposal_rubric_xor_check() -> None:
    rub = Rubric(
        items=[RubricItem(id="a", text="ok", required=True, check="judge")],
        aggregate="all_required",
    )
    with pytest.raises(ValueError, match="exactly one"):
        validate_proposal_content(
            CheckProposal(
                proposal_id="p",
                target="q",
                role="validity",
                measures="m",
                check=_value_check(target=1),
                rubric=rub,
                justification="j",
                rejected_templates=_rejected(),
            )
        )


def test_nfkc_digest_composed_decomposed_same() -> None:
    """Same logical text (NFC vs NFD) → same digest; NFKC-divergent texts differ."""
    nfc = "café"
    nfd = "cafe\u0301"
    assert unicodedata.normalize("NFC", nfc) != nfd or nfc != nfd
    assert unicodedata.normalize("NFKC", nfc) == unicodedata.normalize("NFKC", nfd)
    base = dict(
        proposal_id="p",
        target="q",
        role="prediction",
        check=_value_check(target=1),
        justification="j",
        rejected_templates=_rejected(),
    )
    d1 = compute_proposal_digest(CheckProposal(**base, measures=nfc))
    d2 = compute_proposal_digest(CheckProposal(**base, measures=nfd))
    assert d1 == d2
    d3 = compute_proposal_digest(CheckProposal(**base, measures="other"))
    assert d3 != d1
    # Targets likewise NFKC-normalized in the prereg body.
    t1 = compute_proposal_digest(CheckProposal(**{**base, "measures": "m", "target": nfc}))
    t2 = compute_proposal_digest(CheckProposal(**{**base, "measures": "m", "target": nfd}))
    assert t1 == t2


@pytest.mark.asyncio
async def test_unicode_measures_and_target(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_pr20_uni")
    try:
        await _create(rt, "unicode")
        proposer_aid = await _boot_plan(rt, [_exp_spec()])
        exp_tid = _task_id(rt, "EXP")
        measures = "Δcount ≥ 10²"
        target = "вопрос-α"
        pid = await propose_check(
            rt,
            target=target,
            role="prediction",
            measures=measures,
            justification="unicode prereg",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=exp_tid,
            attempt_id=proposer_aid,
            open_hitl=False,
        )
        assert pid
        row = rt._check_proposals[pid]
        assert row["measures"] == unicodedata.normalize("NFKC", measures)
        assert row["target"] == unicodedata.normalize("NFKC", target)
        assert row["proposal_digest"] == compute_proposal_digest(row)
    finally:
        await close_runtime(rt)


def test_nan_inf_threshold_rejected_at_validate() -> None:
    with pytest.raises(ValueError, match="finite"):
        validate_proposal_content(
            CheckProposal(
                proposal_id="p",
                target="q",
                role="prediction",
                measures="m",
                check=_value_check(target=1),
                justification="j",
                rejected_templates=_rejected(),
                threshold=float("nan"),
            )
        )
    with pytest.raises(ValueError, match="finite"):
        validate_proposal_content(
            CheckProposal(
                proposal_id="p",
                target="q",
                role="prediction",
                measures="m",
                check=_value_check(target=1),
                justification="j",
                rejected_templates=_rejected(),
                sesoi=math.inf,
            )
        )


def test_fold_rejects_rejected_to_approved_transition() -> None:
    """Unit: fold CheckRejected then CheckApproved leaves status rejected."""
    rt = SimpleNamespace(_check_proposals={}, _proposed_check_reviews={})
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pid = "prop_fold"
    fold_proposed_check_event(
        rt,
        EventEnvelope(
            study_id="s",
            type="CheckProposed",
            entity_key=pid,
            idem_key=make_idem("CheckProposed", pid, "d"),
            payload={
                "proposal_id": pid,
                "proposal_digest": "d",
                "task_id": "s/EXP",
                "target": "q",
            },
            ts=now,
            seq=1,
        ),
    )
    fold_proposed_check_event(
        rt,
        EventEnvelope(
            study_id="s",
            type="CheckRejected",
            entity_key=pid,
            idem_key=make_idem("CheckRejected", pid),
            payload={
                "proposal_id": pid,
                "proposal_digest": "d",
                "by": "timeout",
                "reason": "human_timeout",
            },
            ts=now,
            seq=2,
        ),
    )
    assert rt._check_proposals[pid]["status"] == "rejected"
    fold_proposed_check_event(
        rt,
        EventEnvelope(
            study_id="s",
            type="CheckApproved",
            entity_key=pid,
            idem_key=make_idem("CheckApproved", pid),
            payload={
                "proposal_id": pid,
                "proposal_digest": "d",
                "by": "human",
                "source": "api",
                "delegated": False,
            },
            ts=now,
            seq=3,
        ),
    )
    assert rt._check_proposals[pid]["status"] == "rejected"
