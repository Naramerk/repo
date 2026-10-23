"""M1 PR20: proposed_check catalog meta-template (DESIGN_M1 §16)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coscientist.checks.catalog import minimal_test_catalog
from coscientist.checks.evaluate import evaluate_task_checks
from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.claims import RuntimeClaim
from coscientist.core.domain import (
    PROPOSED_CHECK_TEMPLATE_ID,
    AgentTaskOutput,
    ArtifactHandle,
    CheckProposal,
    CodeCheck,
    QuestionSpec,
    RejectedTemplateNote,
    ReqStatus,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.errors import StudyClosedError
from coscientist.core.events import STUDY_CREATED_SCHEMA_VERSION, STUDY_FINALIZED_SCHEMA_VERSION
from coscientist.core.finalize import finalize
from coscientist.core.proposed_check import (
    approve_check,
    compute_proposal_digest,
    propose_check,
    reject_check,
    requirement_ref_for_proposal,
    validate_proposal_content,
)
from coscientist.core.reasons import TaskReason
from coscientist.core.rubric_types import Rubric, RubricItem
from coscientist.core.upcasters import UPCASTERS, register_upcaster, upcast
from coscientist.core.verdict import claim_is_severe, exclusion_reason, hypothesis_verdict
from tests.helpers import attach_executor, close_runtime, open_runtime


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


async def _create(rt, request: str = "pr20", **kwargs):
    await rt.create_study(
        profile="t",
        request=request,
        catalog_version="0.1.0",
        **kwargs,
    )


async def _boot_plan(rt, tasks, *, questions=None):
    """Cancel SPEC/HYP/LIT then accept_plan (same pattern as PR9 tests)."""
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(tasks, questions=questions or [])


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


@pytest.mark.asyncio
async def test_propose_preregisters_digest_before_result(tmp_path: Path) -> None:
    """CheckProposed is written with a fixed digest before any result."""
    rt = await open_runtime(tmp_path, "st_pr20_pre")
    try:
        await _create(rt, "propose digest")
        await _boot_plan(
            rt,
            [
                TaskSpec(
                    task_key="EXP",
                    title="e",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    addresses=["q1"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                    expected_outputs=[],
                )
            ],
            questions=[QuestionSpec(id="q1", text="Q?")],
        )
        aid = await rt.start_attempt("EXP")
        assert aid
        task_id = rt.attempts[aid]["task_id"]
        pid = await propose_check(
            rt,
            target="q1",
            role="prediction",
            measures="count equals 42",
            justification="catalog templates assume fixed ontology metrics",
            rejected_templates=_rejected(),
            check=_value_check(target=42),
            task_id=task_id,
            attempt_id=aid,
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
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_unapproved_check_excluded_from_verdict(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_pr20_unapp")
    try:
        await _create(rt, "unapproved")
        # Placeholder catalog criterion so accept_plan passes PlanValidator;
        # swapped to proposed_check after propose (production would expand).
        await _boot_plan(
            rt,
            [
                TaskSpec(
                    task_key="EXP",
                    title="e",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    addresses=["q1"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                    expected_outputs=[],
                )
            ],
            questions=[QuestionSpec(id="q1", text="Q?")],
        )
        aid = await rt.start_attempt("EXP")
        assert aid
        task_id = rt.attempts[aid]["task_id"]
        pid = await propose_check(
            rt,
            target="q1",
            role="prediction",
            measures="count",
            justification="need custom threshold",
            rejected_templates=_rejected(),
            check=_value_check(target=42),
            task_id=task_id,
            attempt_id=aid,
            open_hitl=False,
        )
        assert pid
        task = rt.tasks[task_id]
        rt.tasks[task_id] = task.model_copy(
            update={
                "spec": task.spec.model_copy(
                    update={"criteria": [requirement_ref_for_proposal(pid)]}
                )
            }
        )
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
        await _boot_plan(
            rt,
            [
                TaskSpec(
                    task_key="EXP",
                    title="e",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    addresses=["q1"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                    expected_outputs=[],
                )
            ],
            questions=[QuestionSpec(id="q1", text="Q?")],
        )
        aid = await rt.start_attempt("EXP")
        assert aid
        task_id = rt.attempts[aid]["task_id"]
        pid = await propose_check(
            rt,
            target="q1",
            role="prediction",
            measures="count==42",
            justification="custom",
            rejected_templates=_rejected(),
            check=_value_check(target=42),
            task_id=task_id,
            attempt_id=aid,
            open_hitl=False,
        )
        assert pid
        await approve_check(rt, pid, by="human", delegated=False)
        assert rt._check_proposals[pid]["status"] == "approved"
        task = rt.tasks[task_id]
        rt.tasks[task_id] = task.model_copy(
            update={
                "spec": task.spec.model_copy(
                    update={
                        "criteria": [
                            requirement_ref_for_proposal(pid),
                            RequirementRef(template_id="artifact_present"),
                        ]
                    }
                )
            }
        )
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
        # Result return omits check_results; authoritative copy is ResultRecorded.
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
    v_all, _, rule = hypothesis_verdict(
        [catalog_refute, catalog_support, delegated_extra],
        refutations_needed=1,
        supported_needed=2,
        refutation_priority=True,
    )
    assert v_all != "supported" or v_cat == "supported"
    assert v_all == v_cat or rule == "k_ref"


@pytest.mark.asyncio
async def test_proposal_quota_closes_task(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_pr20_quota")
    try:
        await _create(rt, "quota", max_proposals=1)
        assert rt.study is not None
        assert rt.study.max_proposals == 1
        await _boot_plan(
            rt,
            [
                TaskSpec(
                    task_key="EXP",
                    title="e",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                    expected_outputs=[],
                )
            ],
        )
        aid = await rt.start_attempt("EXP")
        assert aid
        task_id = rt.attempts[aid]["task_id"]
        pid1 = await propose_check(
            rt,
            target="study",
            role="validity",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=task_id,
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
            task_id=task_id,
            attempt_id=aid,
            open_hitl=False,
        )
        assert pid2 is None
        task = rt.tasks[task_id]
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
async def test_reject_and_timeout_still_finalize(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_pr20_rej")
    try:
        await _create(rt, "reject")
        await _boot_plan(
            rt,
            [
                TaskSpec(
                    task_key="EXP",
                    title="e",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                    expected_outputs=[],
                )
            ],
        )
        aid = await rt.start_attempt("EXP")
        assert aid
        task_id = rt.attempts[aid]["task_id"]
        pid = await propose_check(
            rt,
            target="study",
            role="validity",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=task_id,
            attempt_id=aid,
            open_hitl=False,
        )
        assert pid
        await reject_check(rt, pid, by="policy", reason="human_timeout")
        assert rt._check_proposals[pid]["status"] == "rejected"
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
async def test_no_append_after_study_finalized(tmp_path: Path) -> None:
    """CheckProposed / CheckApproved / CheckRejected rejected after StudyFinalized."""
    rt = await open_runtime(tmp_path, "st_pr20_closed")
    try:
        await _create(rt, "closed")
        await _boot_plan(
            rt,
            [
                TaskSpec(
                    task_key="EXP",
                    title="e",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                    expected_outputs=[],
                )
            ],
        )
        aid = await rt.start_attempt("EXP")
        assert aid
        task_id = rt.attempts[aid]["task_id"]
        pid = await propose_check(
            rt,
            target="study",
            role="validity",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=task_id,
            attempt_id=aid,
            open_hitl=False,
        )
        assert pid
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
                task_id=task_id,
                attempt_id=aid,
                open_hitl=False,
            )
        with pytest.raises(StudyClosedError):
            await approve_check(rt, pid, by="human")
        with pytest.raises(StudyClosedError):
            await reject_check(rt, pid, by="policy", reason="too_late")
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
        await _boot_plan(
            rt,
            [
                TaskSpec(
                    task_key="EXP",
                    title="e",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                    expected_outputs=[],
                )
            ],
        )
        aid = await rt.start_attempt("EXP")
        assert aid
        task_id = rt.attempts[aid]["task_id"]
        pid = await propose_check(
            rt,
            target="study",
            role="prediction",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=7),
            task_id=task_id,
            attempt_id=aid,
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
        await approve_check(rt2, pid, by="human")
        assert rt2._check_proposals[pid]["status"] == "approved"
    finally:
        await close_runtime(rt2)


@pytest.mark.asyncio
async def test_delegated_reviewer_must_differ_from_proposer(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st_pr20_del")
    try:
        await _create(rt, "delegated")
        await _boot_plan(
            rt,
            [
                TaskSpec(
                    task_key="EXP",
                    title="e",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                    expected_outputs=[],
                )
            ],
        )
        aid = await rt.start_attempt("EXP")
        assert aid
        task_id = rt.attempts[aid]["task_id"]
        pid = await propose_check(
            rt,
            target="study",
            role="prediction",
            measures="m",
            justification="j",
            rejected_templates=_rejected(),
            check=_value_check(target=1),
            task_id=task_id,
            attempt_id=aid,
            open_hitl=False,
        )
        assert pid
        with pytest.raises(ValueError, match="differ"):
            await approve_check(
                rt,
                pid,
                delegated=True,
                reviewer_attempt_id=aid,
            )
        await approve_check(
            rt,
            pid,
            delegated=True,
            reviewer_attempt_id=f"{aid}:reviewer",
        )
        assert rt._check_proposals[pid]["delegated"] is True
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
    # Tip-1 → tip: only max_proposals (as_of already present).
    v12, p12 = upcast(
        "StudyCreated", 11, {"study_id": "s", "as_of": None, "user_input_ids": []}
    )
    assert v12 == 12
    assert p12.get("max_proposals") == 3
    assert 10 in UPCASTERS["StudyCreated"] and 11 in UPCASTERS["StudyCreated"]


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
