"""M1 PR15: needs_more_data + experiment_design; contradicts; derivation."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.core.artifact_stamp import stamp_kind
from coscientist.core.artifacts import make_tool_ref
from coscientist.core.claims import RuntimeClaim
from coscientist.core.contradicts import (
    contradiction_pairs,
    contradicts_edge_id,
    contradicts_events_for_new_claims,
    render_contradiction_map,
)
from coscientist.core.derivation import (
    apply_derivation_ceiling,
    by_theory_mark,
    derivation_caps_inconclusive,
    executable_check_met,
)
from coscientist.core.dispose import DisposeRejected, QuestionDisposal, validate_disposal
from coscientist.core.dispositions import dispositions
from coscientist.core.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    OutputSpec,
    QuestionSpec,
    ReqStatus,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.errors import ArtifactRejected
from coscientist.core.events import (
    PLAN_ACCEPTED_SCHEMA_VERSION,
    PLAN_EXPANDED_SCHEMA_VERSION,
    TASK_CREATED_SCHEMA_VERSION,
)
from coscientist.core.experiment_design import (
    ARTIFACT_KIND_EXPERIMENT_DESIGN,
    EXPERIMENT_DESIGN_ITEM_IDS,
    MISSING_DESIGN_REASON,
    design_artifact_ok,
    experiment_design_rubric,
    is_experiment_design_rubric,
)
from coscientist.core.graph import GraphEdge, GraphNode, GraphStore, registry_from_schema
from coscientist.core.plan_validate import validate_output_kinds
from coscientist.core.reasons import Disposition
from coscientist.core.rubric_types import Rubric, RubricItem
from coscientist.core.upcasters import (
    UPCASTERS,
    assert_upcaster_chain_continuous,
    register_upcaster,
    writer_schema_version,
)
from coscientist.core.verdict import hypothesis_verdict
from tests.helpers import attach_executor, close_runtime, open_runtime


def _assert_terminated(events: list[Any]) -> Any:
    """Exactly one StudyFinalized; report XOR minimal_report; non-empty conclusion_ids."""
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1
    fin = fins[0]
    has_art = fin.payload.get("report_artifact_id") is not None
    has_min = fin.payload.get("minimal_report") is not None
    assert has_art ^ has_min, "report_artifact_id XOR minimal_report"
    cids = fin.payload.get("conclusion_ids")
    assert isinstance(cids, list) and len(cids) > 0, "conclusion_ids must be non-empty"
    return fin


def _design_rubric() -> Rubric:
    return experiment_design_rubric(judge=False)


def _exp_design(key: str, *, addresses: list[str]) -> TaskSpec:
    rub = _design_rubric()
    return TaskSpec(
        task_key=key,
        title="design",
        goal="propose experiment",
        route="experiment",
        depends_on=["PLAN"],
        addresses=addresses,
        expected_outputs=[
            OutputSpec(
                name="main",
                media_type="text/markdown",
                role="report",
                rubric=rub,
                kind="experiment_design",
            )
        ],
        criteria=[RequirementRef(template_id="rubric_check")],
    )


async def _boot(tmp_path, clock, sid: str):
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="t",
        request="what dose works?",
        catalog_version="1",
        deadline=clock.now() + timedelta(hours=1),
        finalize_reserve_s=30,
        sid=sid,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    return rt


async def _complete_report_and_finalize(rt, tmp_path) -> None:
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


# --- pure helpers ------------------------------------------------------------


def test_experiment_design_rubric_ids():
    rub = _design_rubric()
    assert is_experiment_design_rubric(rub)
    assert EXPERIMENT_DESIGN_ITEM_IDS <= {it.id for it in rub.items if it.required}


def test_output_spec_kind_enforced_by_plan_validator():
    """Explicit kind required; spoofed rubric ids without kind are refused for kind path."""
    # Valid kind + rubric.
    validate_output_kinds([_exp_design("E0", addresses=["Q1"])])
    # kind without matching rubric → refuse.
    bad = TaskSpec(
        task_key="E1",
        title="x",
        goal="x",
        route="experiment",
        depends_on=["PLAN"],
        addresses=["Q1"],
        expected_outputs=[
            OutputSpec(
                name="main",
                media_type="text/markdown",
                role="report",
                kind="experiment_design",
                rubric=Rubric(
                    items=[
                        RubricItem(
                            id="only",
                            text="nope",
                            required=True,
                            check="code",
                            code_kind="artifact_exists",
                        ),
                    ],
                    aggregate="all_required",
                ),
            )
        ],
    )
    with pytest.raises(ValueError, match="experiment_design"):
        validate_output_kinds([bad])
    # Invalid kind literal rejected by pydantic.
    with pytest.raises(ValidationError):
        OutputSpec(
            name="main",
            media_type="text/markdown",
            role="report",
            kind="not_a_kind",  # type: ignore[arg-type]
        )


def test_needs_more_data_requires_design_validation():
    d = QuestionDisposal(
        question_id="Q1",
        disposition="needs_more_data",
        reason="insufficient power",
        refs=["evidence:e1"],
        design_artifact_id=None,
    )
    with pytest.raises(DisposeRejected) as ei:
        validate_disposal(d, known_question_ids={"Q1"})
    assert MISSING_DESIGN_REASON in str(ei.value)


def test_infeasible_disposal_ok_without_design():
    d = QuestionDisposal(
        question_id="Q1",
        disposition="infeasible",
        reason="no assay",
        refs=["hitl:h1"],
    )
    validate_disposal(d, known_question_ids={"Q1"})


def test_refs_require_known_prefix():
    d = QuestionDisposal(
        question_id="Q1",
        disposition="infeasible",
        reason="x",
        refs=["zz"],  # 2 chars but no prefix
    )
    with pytest.raises(DisposeRejected, match="ref"):
        validate_disposal(d, known_question_ids={"Q1"})


def test_design_artifact_ok_fallback_binds_to_task(tmp_path):
    """Fallback without stamped kind must bind artifact_id to covering task outputs."""
    arts = __import__("coscientist.core.artifacts", fromlist=["LocalArtifactStore"]).LocalArtifactStore(
        tmp_path / "arts"
    )
    art = arts.put(
        b"design",
        make_tool_ref("a:1"),
        name="main",
        media_type="text/markdown",
        role="report",
    )
    # Unstamped + no results → False.
    assert design_artifact_ok(
        artifact_id=art.artifact_id,
        artifact_lookup=arts.get_meta,
        tasks={},
        results={},
        task_keys={"E0"},
    ) is False
    # Stamped kind → True.
    stamp_kind(arts, art.artifact_id, ARTIFACT_KIND_EXPERIMENT_DESIGN)
    assert design_artifact_ok(
        artifact_id=art.artifact_id,
        artifact_lookup=arts.get_meta,
        tasks={},
        results={},
        task_keys={"E0"},
    ) is True


def test_stamp_kind_rejects_invalid():
    with pytest.raises(ArtifactRejected):
        stamp_kind(object(), "art_x", "nope")


def test_question_disposed_fold_first_wins():
    from coscientist.core.dispose import fold_question_disposed

    store: dict = {}
    fold_question_disposed(
        store,
        {
            "question_id": "Q1",
            "disposition": "infeasible",
            "reason": "first",
            "refs": ["hitl:h1"],
        },
    )
    fold_question_disposed(
        store,
        {
            "question_id": "Q1",
            "disposition": "needs_more_data",
            "reason": "second",
            "refs": ["evidence:e1"],
            "design_artifact_id": "art_x",
        },
    )
    assert store["Q1"]["disposition"] == "infeasible"
    assert store["Q1"]["reason"] == "first"


@pytest.mark.asyncio
async def test_needs_more_data_requires_design(tmp_path, clock):
    """Without experiment_design artifact, needs_more_data does not stick."""
    rt = await _boot(tmp_path, clock, "st_nmd1")
    await rt.accept_plan(
        tasks=[_exp_design("E0", addresses=["Q1"])],
        questions=[QuestionSpec(id="Q1", text="dose?", final_type="artifact")],
    )
    # Bind artifacts store on runtime for dispose validation.
    ex, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts

    # Dispose without design → rejected; disposition stays not_attempted.
    got = await rt.dispose_question(
        {
            "question_id": "Q1",
            "disposition": "needs_more_data",
            "reason": "need RCT",
            "refs": ["evidence:e1"],
        }
    )
    assert got is None
    assert "Q1" not in rt.question_disposals
    assert getattr(rt, "_disposal_rejects", None)

    # Core-stamped experiment_design artifact unlocks needs_more_data.
    art = arts.put_core(
        b"# design\nhypothesis / variables / n / criterion\n",
        make_tool_ref("core:design"),
        name="main",
        media_type="text/markdown",
        role="report",
        kind=ARTIFACT_KIND_EXPERIMENT_DESIGN,
    )
    got2 = await rt.dispose_question(
        QuestionDisposal(
            question_id="Q1",
            disposition="needs_more_data",
            reason="need RCT",
            refs=["evidence:e1"],
            design_artifact_id=art.artifact_id,
        )
    )
    assert got2 == "Q1"
    assert rt.question_disposals["Q1"]["disposition"] == "needs_more_data"

    # Covering task can fail; disposal still honest.
    await rt.start_attempt("E0")
    await rt.cancel_task("E0", reason="cancelled")
    await _complete_report_and_finalize(rt, tmp_path)
    assert rt.view().phase == "closed"
    events = await rt.store.read_all()
    _assert_terminated(events)
    d = dispositions(
        tasks=rt.tasks,
        plans=rt.plans,
        graph=rt.graph,
        study=rt.study,
        results=rt.results,
        artifact_lookup=arts.get_meta,
        question_disposals=rt.question_disposals,
    )
    assert d["Q1"]["disposition"] == Disposition.NEEDS_MORE_DATA.value
    assert d["Q1"].get("design_artifact_id") == art.artifact_id
    # Kill / reopen replay
    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, "st_nmd1", clock=clock, owner="t1")
    assert "Q1" in rt2.question_disposals
    assert rt2.question_disposals["Q1"]["disposition"] == "needs_more_data"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_disposal_unknown_question_fail_closed_empty_graph(tmp_path, clock):
    """Empty graph must not invent known ids from the disposal payload."""
    rt = await _boot(tmp_path, clock, "st_unk")
    # Plan with no questions / addresses — disposal must reject.
    await rt.accept_plan(tasks=[], questions=[])
    ex, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("REPORT")
    assert aid is not None
    art = arts.put(
        b"r",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="text/markdown",
    )
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="r",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            question_disposals=[
                {
                    "question_id": "QX",
                    "disposition": "infeasible",
                    "reason": "spoof",
                    "refs": ["hitl:h1"],
                }
            ],
        ),
    )
    assert "QX" not in rt.question_disposals
    assert any("unknown question" in str(n.get("detail", "")) for n in getattr(ex, "_disposal_rejects", []))
    # REPORT completion finalizes; assert honest termination shape.
    await rt.ensure_lease(ttl_s=10_000)
    await rt.tick()
    if rt.view().phase == "closed":
        _assert_terminated(await rt.store.read_all())
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_contradicts_edge(tmp_path, clock):
    registry_from_schema.cache_clear()
    reg = registry_from_schema()
    g = GraphStore(reg)
    sid = "st_c1"
    now = clock.now()
    qid = f"{sid}:question:Q1"
    g.upsert_node(
        GraphNode(node_id=qid, kind="question", study_id=sid, ref_id="Q1", attrs={})
    )
    for i, status in enumerate(("met", "not_met")):
        cid = f"{sid}:claim:c{i}"
        g.upsert_node(
            GraphNode(
                node_id=cid,
                kind="claim",
                study_id=sid,
                ref_id=cid,
                attrs={
                    "accepted": True,
                    "frozen": True,
                    "criterion_status": status,
                    "target": "Q1",
                },
            )
        )
        g.add_edge(
            GraphEdge(
                edge_id=f"about:{cid}",
                kind="about",
                study_id=sid,
                src_id=cid,
                dst_id=qid,
                attrs={},
            )
        )
    pairs = contradiction_pairs(g, study_id=sid)
    assert len(pairs) == 1
    a, b, _tgt = pairs[0]
    assert a < b
    evs = contradicts_events_for_new_claims(
        study_id=sid, graph=g, now=now, new_claim_ids={a, b}
    )
    assert len(evs) == 1
    assert evs[0].type == "GraphEdgeAdded"
    assert evs[0].payload["kind"] == "contradicts"
    eid = contradicts_edge_id(sid, a, b)
    assert evs[0].payload["edge_id"] == eid
    # Idempotent: second emit empty once edge present.
    g.add_edge(GraphEdge.model_validate(evs[0].payload))
    evs2 = contradicts_events_for_new_claims(
        study_id=sid, graph=g, now=now, new_claim_ids={a, b}
    )
    assert evs2 == []
    raw, dig = render_contradiction_map(g, study_id=sid)
    assert dig
    text = raw.decode()
    assert '"count":1' in text


def test_derivation_verdict_marked():
    """Review-only derivation never counts; executable lifts; by_theory marks both."""
    review_only = RuntimeClaim(
        criterion_status="met",
        accepted=True,
        evidence_kind="derivation",
        provenance="real",
        frozen=True,
        claim_outcome="consistent",
        severe=True,
        validity_ok=True,
        check_role="prediction",
        derivation_executable=False,
        independence_key="d1",
        attempt_id="E0#1",
        intent="confirm",
        uses_sesoi=True,
        used_sesoi=True,
        sesoi=0.1,
        effect={"countable": True, "est": 0.5, "ci_low": 0.2, "ci_high": 0.8},
    )
    assert review_only.counts() is False
    assert derivation_caps_inconclusive([review_only])
    assert by_theory_mark([review_only])
    assert apply_derivation_ceiling("supported", [review_only]) == "inconclusive"
    v, _lit, by_th, rule = hypothesis_verdict(
        [review_only],
        True,
        supported_needed=2,
        refutations_needed=1,
        refutation_priority=False,
    )
    assert v == "inconclusive"
    assert by_th is True
    assert rule == "derivation_review_only"

    # Mixed: review-only derivation must not feed k_sup alongside computational.
    comp = RuntimeClaim(
        criterion_status="met",
        accepted=True,
        evidence_kind="computational",
        provenance="real",
        frozen=True,
        claim_outcome="consistent",
        severe=True,
        validity_ok=True,
        check_role="prediction",
        independence_key="c1",
        attempt_id="C0#1",
        intent="confirm",
        uses_sesoi=True,
        used_sesoi=True,
        sesoi=0.1,
        effect={"countable": True, "est": 0.5, "ci_low": 0.2, "ci_high": 0.8},
    )
    # One severe computational + review-only derivation → survived (not inflated to supported).
    v_mix, _l, by_th_mix, _r = hypothesis_verdict(
        [comp, review_only],
        True,
        supported_needed=2,
        refutations_needed=1,
        refutation_priority=False,
    )
    assert v_mix == "survived"
    assert by_th_mix is True

    exe = RuntimeClaim(
        criterion_status="met",
        accepted=True,
        evidence_kind="derivation",
        provenance="real",
        frozen=True,
        claim_outcome="consistent",
        severe=True,
        validity_ok=True,
        check_role="prediction",
        derivation_executable=True,
        independence_key="d2",
        attempt_id="E1#1",
        intent="refute",
        uses_sesoi=True,
        used_sesoi=True,
        sesoi=0.1,
        refute_capable=True,
        effect={"countable": True, "est": 0.5, "ci_low": 0.2, "ci_high": 0.8},
    )
    a = RuntimeClaim(
        criterion_status="met",
        accepted=True,
        evidence_kind="derivation",
        provenance="real",
        frozen=True,
        claim_outcome="consistent",
        severe=True,
        validity_ok=True,
        check_role="prediction",
        derivation_executable=True,
        independence_key="d3",
        attempt_id="E2#1",
        intent="confirm",
        uses_sesoi=True,
        used_sesoi=True,
        sesoi=0.1,
        effect={"countable": True, "est": 0.5, "ci_low": 0.2, "ci_high": 0.8},
    )
    v2, _l2, by_th2, rule2 = hypothesis_verdict(
        [a, exe],
        True,
        supported_needed=2,
        refutations_needed=1,
        refutation_priority=False,
    )
    assert v2 == "supported"
    assert by_th2 is True
    assert rule2 == "support_min"


def test_executable_check_met_excludes_presence_and_waived():
    row = type("C", (), {"kind": "exec_runs", "status": ReqStatus.WAIVED})()
    assert executable_check_met([row]) is False
    presence = type("C", (), {"kind": "artifact_exists", "status": ReqStatus.MET})()
    assert executable_check_met([presence]) is False
    real = type("C", (), {"kind": "exec_runs", "status": ReqStatus.MET})()
    assert executable_check_met([real]) is True


def test_upcaster_chains_continuous_and_no_overwrite():
    """Checklist: chains 1..N-1 continuous vs writer versions; no registry mutate."""
    for et in (
        "TaskCreated",
        "PlanAccepted",
        "PlanExpanded",
        "AttemptStarted",
        "ResultRecorded",
        "StudyCreated",
        "StudyFinalized",
        "TaskDone",
        "AttemptClosed",
        "QuestionDisposed",
    ):
        w = writer_schema_version(et)
        assert_upcaster_chain_continuous(et, w)
    # Writers for plan events match registered chain ends (PR15 OutputSpec.kind).
    assert writer_schema_version("TaskCreated") == TASK_CREATED_SCHEMA_VERSION == 12
    assert writer_schema_version("PlanAccepted") == PLAN_ACCEPTED_SCHEMA_VERSION == 11
    assert writer_schema_version("PlanExpanded") == PLAN_EXPANDED_SCHEMA_VERSION == 5
    assert writer_schema_version("QuestionDisposed") == 1
    # Duplicate guard without mutating production event registries.
    register_upcaster("_pr15_dup_guard", 1, lambda p: p)
    with pytest.raises(ValueError, match="duplicate upcaster"):
        register_upcaster("_pr15_dup_guard", 1, lambda p: p)
    UPCASTERS.pop("_pr15_dup_guard", None)


@pytest.mark.asyncio
async def test_impossible_task_still_closes_with_report(tmp_path, clock):
    """Every study closes with a report even for nonsensical plans."""
    rt = await _boot(tmp_path, clock, "st_imp")
    # Empty plan (no questions) — still finalizes with a report.
    await rt.accept_plan(tasks=[], questions=[])
    await _complete_report_and_finalize(rt, tmp_path)
    assert rt.view().phase == "closed"
    assert rt.view().verdict is not None
    _assert_terminated(await rt.store.read_all())
    await close_runtime(rt)
