"""M1 PR15: needs_more_data + experiment_design; contradicts; derivation."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.core.claims.claims import RuntimeClaim
from coscientist.core.claims.contradicts import (
    contradiction_pairs,
    contradicts_edge_id,
    contradicts_events_for_new_claims,
    render_contradiction_map,
)
from coscientist.core.claims.derivation import (
    apply_derivation_ceiling,
    by_theory_mark,
    derivation_caps_inconclusive,
    executable_check_met,
)
from coscientist.core.claims.dispose import DisposeRejected, QuestionDisposal, validate_disposal
from coscientist.core.claims.dispositions import dispositions
from coscientist.core.claims.experiment_design import (
    ARTIFACT_KIND_EXPERIMENT_DESIGN,
    EXPERIMENT_DESIGN_ITEM_IDS,
    MISSING_DESIGN_REASON,
    design_artifact_ok,
    experiment_design_rubric,
    is_experiment_design_rubric,
)
from coscientist.core.claims.graph import GraphEdge, GraphNode, GraphStore, registry_from_schema
from coscientist.core.claims.reasons import Disposition
from coscientist.core.claims.rubric_types import Rubric, RubricItem
from coscientist.core.claims.verdict import hypothesis_verdict
from coscientist.core.events.errors import ArtifactRejected
from coscientist.core.events.events import (
    PLAN_ACCEPTED_SCHEMA_VERSION,
    PLAN_EXPANDED_SCHEMA_VERSION,
    TASK_CREATED_SCHEMA_VERSION,
)
from coscientist.core.events.upcasters import (
    UPCASTERS,
    assert_upcaster_chain_continuous,
    register_upcaster,
    upcast,
    writer_schema_version,
)
from coscientist.core.execution.artifact_stamp import apply_kind_stamp
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    OutputSpec,
    QuestionSpec,
    ReqStatus,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.plan.plan_validate import validate_evidence_kinds, validate_output_kinds
from tests.helpers import attach_executor, close_runtime, create_study, open_runtime


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
    await create_study(rt,
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
        role="report",
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


def test_design_artifact_ok_ignores_meta_kind_without_journal(tmp_path):
    """meta.kind alone must not unlock needs_more_data (pre-commit stamp spoof)."""
    arts = __import__("coscientist.core.execution.artifacts", fromlist=["LocalArtifactStore"]).LocalArtifactStore(
        tmp_path / "arts"
    )
    art = arts.put(
        b"design",
        make_tool_ref("a:1"),
        name="main",
        media_type="text/markdown",
        role="report",
    )
    apply_kind_stamp(arts, art.artifact_id, ARTIFACT_KIND_EXPERIMENT_DESIGN)
    assert arts.get_meta(art.artifact_id).kind == ARTIFACT_KIND_EXPERIMENT_DESIGN
    # Without journal_kinds / pending / covering-task → False.
    assert design_artifact_ok(
        artifact_id=art.artifact_id,
        artifact_lookup=arts.get_meta,
        tasks={},
        results={},
        task_keys={"E0"},
    ) is False
    assert design_artifact_ok(
        artifact_id=art.artifact_id,
        journal_kinds={art.artifact_id: ARTIFACT_KIND_EXPERIMENT_DESIGN},
    ) is True
    assert design_artifact_ok(
        artifact_id=art.artifact_id,
        pending_design_ids={art.artifact_id},
    ) is True


def test_apply_kind_stamp_rejects_invalid_and_missing(tmp_path):
    from coscientist.core.execution.artifacts import LocalArtifactStore

    with pytest.raises(ArtifactRejected):
        apply_kind_stamp(object(), "art_x", "nope")
    arts = LocalArtifactStore(tmp_path / "arts")
    with pytest.raises(ArtifactRejected, match="missing"):
        apply_kind_stamp(arts, "art_missing", ARTIFACT_KIND_EXPERIMENT_DESIGN)


def test_evidence_kind_explicit_not_inferred_from_rubric_ids():
    """steps+assumptions rubric alone must NOT imply derivation (§6.6)."""
    from coscientist.core.claims.derivation import is_derivation_task_spec

    rub = Rubric(
        items=[
            RubricItem(id="steps", text="s", required=True, check="code", code_kind="artifact_exists"),
            RubricItem(
                id="assumptions", text="a", required=True, check="code", code_kind="artifact_exists"
            ),
        ],
        aggregate="all_required",
    )
    spoof = TaskSpec(
        task_key="D0",
        title="t",
        goal="g",
        route="experiment",
        depends_on=["PLAN"],
        addresses=["Q1"],
        expected_outputs=[
            OutputSpec(name="main", media_type="text/plain", role="code", rubric=rub)
        ],
    )
    assert is_derivation_task_spec(spoof) is False
    with pytest.raises(ValidationError):
        OutputSpec(
            name="main",
            media_type="text/plain",
            role="code",
            evidence_kind="not_a_kind",  # type: ignore[arg-type]
        )
    bad_role = TaskSpec(
        task_key="D1",
        title="t",
        goal="g",
        route="experiment",
        depends_on=["PLAN"],
        addresses=["Q1"],
        expected_outputs=[
            OutputSpec(
                name="main",
                media_type="text/plain",
                role="data",
                evidence_kind="derivation",
            )
        ],
    )
    with pytest.raises(ValueError, match="derivation"):
        validate_evidence_kinds([bad_role])
    ok = TaskSpec(
        task_key="D2",
        title="t",
        goal="g",
        route="experiment",
        depends_on=["PLAN"],
        addresses=["Q1"],
        expected_outputs=[
            OutputSpec(
                name="main",
                media_type="text/plain",
                role="code",
                evidence_kind="derivation",
            )
        ],
    )
    validate_evidence_kinds([ok])
    assert is_derivation_task_spec(ok) is True


def test_executable_check_met_only_bound_check():
    """Unrelated MET executable must not lift §6.6 for this claim."""
    met = type("C", (), {"kind": "exec_runs", "status": ReqStatus.MET, "template_id": "other", "check_id": "c1"})()
    bound = type(
        "C", (), {"kind": "exec_runs", "status": ReqStatus.MET, "template_id": "mine", "check_id": "c2"}
    )()
    assert executable_check_met([met, bound], bound_key="mine") is True
    assert executable_check_met([met], bound_key="mine") is False
    assert executable_check_met([met, bound], bound_key=None) is False
    assert executable_check_met([met, bound]) is False


def test_question_disposed_fold_first_wins():
    from coscientist.core.claims.dispose import fold_question_disposed

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


def test_honest_terminals_stamp_verdict_for_rollup():
    """Honest terminals use ``terminal=True`` (not disposition strings in ``verdict``).

    needs_more_data / infeasible / ill_posed must not put claim-vocabulary
    pollution into ``verdict``; rollup clamps on ``terminal`` / disposition.
    """
    from coscientist.core.claims.dispositions import stamp_honest_terminal_verdict

    for disp in (
        Disposition.NEEDS_MORE_DATA.value,
        Disposition.INFEASIBLE.value,
        Disposition.ILL_POSED.value,
    ):
        row = stamp_honest_terminal_verdict(
            {"disposition": disp, "reason": disp, "formulation": "q", "verdict": disp}
        )
        assert row.get("terminal") is True
        assert row.get("verdict") is None  # disposition string stripped from verdict
    na = stamp_honest_terminal_verdict(
        {"disposition": Disposition.NOT_ATTEMPTED.value, "reason": "no_task", "formulation": "q"}
    )
    assert na.get("terminal") is not True
    assert na.get("verdict") is None

    plans = {
        "p": {
            "questions": [
                {"id": "Q_nmd", "text": "nmd?", "final_type": "verdict"},
                {"id": "Q_inf", "text": "inf?", "final_type": "verdict"},
            ],
            "tasks": [
                {"task_key": "E_nmd", "addresses": ["Q_nmd"]},
                {"task_key": "E_inf", "addresses": ["Q_inf"]},
            ],
        }
    }
    disposals = {
        "Q_nmd": {
            "disposition": "needs_more_data",
            "reason": "need RCT",
            "refs": ["evidence:e1"],
            "design_artifact_id": "art_design",
        },
        "Q_inf": {
            "disposition": "infeasible",
            "reason": "no assay",
            "refs": ["hitl:h1"],
        },
    }
    got = dispositions(
        tasks={},
        plans=plans,
        question_disposals=disposals,
        journal_kinds={"art_design": ARTIFACT_KIND_EXPERIMENT_DESIGN},
    )
    assert got["Q_nmd"]["disposition"] == Disposition.NEEDS_MORE_DATA.value
    assert got["Q_nmd"].get("terminal") is True
    assert got["Q_nmd"].get("verdict") is None
    assert got["Q_inf"]["disposition"] == Disposition.INFEASIBLE.value
    assert got["Q_inf"].get("terminal") is True
    assert got["Q_inf"].get("verdict") is None
    bad = dispositions(
        tasks={},
        plans={"p": {"questions": [{"id": "Q", "text": "q", "final_type": "verdict"}], "tasks": []}},
        question_disposals={
            "Q": {
                "disposition": "needs_more_data",
                "reason": "x",
                "refs": ["evidence:e1"],
                "design_artifact_id": "missing",
            }
        },
        journal_kinds={},
    )
    assert bad["Q"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert bad["Q"].get("terminal") is not True
    assert bad["Q"].get("verdict") is None

    from coscientist.core.claims.rollup import apply_rollup
    from tests.test_pr16_decomposes_rollup import _tree_plans

    leaves = {
        "Q": {
            "disposition": Disposition.NEEDS_MORE_DATA.value,
            "terminal": True,
            "reason": "need RCT",
            "formulation": "root",
        },
        "Q.a": {
            "disposition": Disposition.ANSWERED.value,
            "verdict": "answered",
            "reason": None,
            "formulation": "a",
        },
        "Q.b": {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "no_task",
            "formulation": "b",
        },
    }
    rolled = apply_rollup(leaves, plans=_tree_plans("all"))
    assert rolled["Q"]["disposition"] == Disposition.NEEDS_MORE_DATA.value
    assert rolled["Q"].get("terminal") is True
    assert rolled["Q"].get("verdict") is None
    assert rolled["Q"].get("rollup_clamped") is True


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
    reject_evs = [e for e in await rt.store.read_all() if e.type == "QuestionDisposeRejected"]
    assert len(reject_evs) >= 1

    # Journaled ArtifactRegistered(stamped) unlocks needs_more_data (not meta alone).
    art = arts.put_core(
        b"# design\nhypothesis / variables / n / criterion\n",
        make_tool_ref("core:design"),
        name="main",
        media_type="text/markdown",
        role="report",
    )
    from coscientist.core.execution.artifact_stamp import kind_stamped_event

    await rt._append(  # noqa: SLF001
        [
            kind_stamped_event(
                study_id="st_nmd1",
                artifact_id=art.artifact_id,
                kind=ARTIFACT_KIND_EXPERIMENT_DESIGN,
                now=clock.now(),
                name=art.name,
                sha256=art.sha256,
                role=art.role,
            )
        ]
    )
    assert rt.artifact_kinds.get(art.artifact_id) == ARTIFACT_KIND_EXPERIMENT_DESIGN
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
        journal_kinds=rt.artifact_kinds,
    )
    assert d["Q1"]["disposition"] == Disposition.NEEDS_MORE_DATA.value
    assert d["Q1"].get("terminal") is True
    assert d["Q1"].get("verdict") is None
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
        role="report",
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
    reject_evs = [e for e in await rt.store.read_all() if e.type == "QuestionDisposeRejected"]
    assert any("unknown question" in str(e.payload.get("detail") or "") for e in reject_evs)
    # REPORT completion finalizes; assert honest termination shape.
    await rt.ensure_lease(ttl_s=10_000)
    await rt.tick()
    assert rt.view().phase == "closed"
    events = await rt.store.read_all()
    fin = _assert_terminated(events)
    contrib = list(fin.payload.get("contributing_reasons") or [])
    assert any("disposal rejected" in str(c) or "не выполнено" in str(c) for c in contrib)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_dispose_rejected_survives_restart(tmp_path, clock):
    """QuestionDisposeRejected is journaled; reopen restores _disposal_rejects + report line."""
    rt = await _boot(tmp_path, clock, "st_rej_rp")
    await rt.accept_plan(
        tasks=[_exp_design("E0", addresses=["Q1"])],
        questions=[QuestionSpec(id="Q1", text="dose?", final_type="artifact")],
    )
    got = await rt.dispose_question(
        {
            "question_id": "Q1",
            "disposition": "needs_more_data",
            "reason": "need design",
            "refs": ["evidence:e1"],
        }
    )
    assert got is None
    assert any(e.type == "QuestionDisposeRejected" for e in await rt.store.read_all())
    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, "st_rej_rp", clock=clock, owner="t1")
    notes = getattr(rt2, "_disposal_rejects", None) or []
    assert notes, "fold must restore disposal rejects after restart"
    assert any(
        "missing_experiment_design" in str(n.get("detail") or "")
        or "design" in str(n.get("report_line") or "")
        for n in notes
    )
    await close_runtime(rt2)


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
                    "target_kind": "question",
                    "checked_against": "value_match",
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
        check_role="prediction",
        target_kind="hypothesis",
        accepted=True,
        evidence_kind="derivation",
        provenance="real",
        frozen=True,
        claim_outcome="consistent",
        severe=True,
        validity_ok=True,
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
        check_role="prediction",
        target_kind="hypothesis",
        accepted=True,
        evidence_kind="computational",
        provenance="real",
        frozen=True,
        claim_outcome="consistent",
        severe=True,
        validity_ok=True,
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
        check_role="prediction",
        target_kind="hypothesis",
        accepted=True,
        evidence_kind="derivation",
        provenance="real",
        frozen=True,
        claim_outcome="consistent",
        severe=True,
        validity_ok=True,
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
        check_role="prediction",
        target_kind="hypothesis",
        accepted=True,
        evidence_kind="derivation",
        provenance="real",
        frozen=True,
        claim_outcome="consistent",
        severe=True,
        validity_ok=True,
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
    row = type(
        "C", (), {"kind": "exec_runs", "status": ReqStatus.WAIVED, "template_id": "x", "check_id": "c"}
    )()
    assert executable_check_met([row], bound_key="x") is False
    presence = type(
        "C",
        (),
        {"kind": "artifact_exists", "status": ReqStatus.MET, "template_id": "x", "check_id": "c"},
    )()
    assert executable_check_met([presence], bound_key="x") is False
    real = type(
        "C", (), {"kind": "exec_runs", "status": ReqStatus.MET, "template_id": "x", "check_id": "c"}
    )()
    assert executable_check_met([real], bound_key="x") is True


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
        "QuestionDisposeRejected",
    ):
        w = writer_schema_version(et)
        assert_upcaster_chain_continuous(et, w)
    # Writers for plan events match registered chain ends (PR15 OutputSpec.kind).
    assert writer_schema_version("TaskCreated") == TASK_CREATED_SCHEMA_VERSION == 13
    assert writer_schema_version("PlanAccepted") == PLAN_ACCEPTED_SCHEMA_VERSION == 11
    assert writer_schema_version("PlanExpanded") == PLAN_EXPANDED_SCHEMA_VERSION == 5
    assert writer_schema_version("QuestionDisposed") == 1
    assert writer_schema_version("QuestionDisposeRejected") == 1
    v, p = upcast(
        "TaskCreated",
        12,
        {
            "task_id": "t",
            "spec": {
                "task_key": "E0",
                "expected_outputs": [{"name": "main", "media_type": "text/plain"}],
            },
        },
    )
    assert v == 13
    assert p["spec"]["expected_outputs"][0].get("evidence_kind") is None
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


@pytest.mark.asyncio
async def test_stamp_kind_not_applied_before_commit_failure(tmp_path, clock):
    """Commit failure must leave meta.kind unset (fold-only stamp)."""
    rt = await _boot(tmp_path, clock, "st_stamp_fail")
    await rt.accept_plan(
        tasks=[_exp_design("E0", addresses=["Q1"])],
        questions=[QuestionSpec(id="Q1", text="dose?", final_type="artifact")],
    )
    ex, arts = attach_executor(rt, tmp_path)
    # Start the design task attempt.
    tid = next(t.task_id for t in rt.tasks.values() if t.spec.task_key == "E0")
    aid = await rt.start_attempt(tid)
    assert aid is not None
    art = arts.put(
        b"# design\n",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="text/markdown",
        role="report",
    )
    assert art.kind is None

    async def _boom(events, **kwargs):
        raise RuntimeError("commit failed")

    ex._append = _boom  # noqa: SLF001
    with pytest.raises(RuntimeError, match="commit failed"):
        await ex.record(
            aid,
            AgentTaskOutput(
                summary="design",
                outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            ),
        )
    meta = arts.get_meta(art.artifact_id)
    assert meta is not None and meta.kind is None
    assert art.artifact_id not in (getattr(rt, "artifact_kinds", {}) or {})
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_stamp_kind_e2e_replay_and_missing_meta_marks_internal_error(tmp_path, clock):
    """Successful stamp folds; missing meta on reopen marks tainted/internal_error.

    Study must still open and reach StudyFinalized (no ArtifactRejected in fold).
    """
    rt = await _boot(tmp_path, clock, "st_stamp_ok")
    # final_type=verdict so rubric MET does not force answered — disposal path is visible.
    await rt.accept_plan(
        tasks=[_exp_design("E0", addresses=["Q1"])],
        questions=[QuestionSpec(id="Q1", text="dose?", final_type="verdict")],
    )
    ex, arts = attach_executor(rt, tmp_path)
    tid = next(t.task_id for t in rt.tasks.values() if t.spec.task_key == "E0")
    aid = await rt.start_attempt(tid)
    assert aid is not None
    art = arts.put(
        b"# design\n",
        make_tool_ref(f"{aid}:main"),
        name="main",
        media_type="text/markdown",
        role="report",
    )
    await ex.record(
        aid,
        AgentTaskOutput(
            summary="design",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            question_disposals=[
                {
                    "question_id": "Q1",
                    "disposition": "needs_more_data",
                    "reason": "need RCT",
                    "refs": ["evidence:e1"],
                    "design_artifact_id": art.artifact_id,
                }
            ],
        ),
    )
    assert rt.artifact_kinds.get(art.artifact_id) == ARTIFACT_KIND_EXPERIMENT_DESIGN
    assert arts.get_meta(art.artifact_id).kind == ARTIFACT_KIND_EXPERIMENT_DESIGN
    assert "Q1" in rt.question_disposals
    # Drop meta before reopen so fold hits missing-meta path.
    meta_path = arts.meta / f"{art.artifact_id}.json"
    meta_path.unlink()
    arts._index.pop(art.artifact_id, None)  # noqa: SLF001
    await close_runtime(rt)

    rt2 = await open_runtime(tmp_path, "st_stamp_ok", clock=clock, owner="t1")
    # Missing meta must not leave artifact_kinds unlocked (needs_more_data would stick).
    assert art.artifact_id not in (getattr(rt2, "artifact_kinds", {}) or {})
    miss = getattr(rt2, "_stamp_meta_missing", None) or []
    assert miss and miss[0].get("internal_error") is True and miss[0].get("tainted") is True
    assert (
        design_artifact_ok(
            artifact_id=art.artifact_id,
            artifact_lookup=arts.get_meta,
            journal_kinds={art.artifact_id: ARTIFACT_KIND_EXPERIMENT_DESIGN},
            tasks=rt2.tasks,
            results=rt2.results,
            task_keys={"E0"},
        )
        is False
    )
    d = dispositions(
        tasks=rt2.tasks,
        plans=rt2.plans,
        graph=rt2.graph,
        study=rt2.study,
        results=rt2.results,
        artifact_lookup=arts.get_meta,
        question_disposals=rt2.question_disposals,
        journal_kinds=rt2.artifact_kinds,
    )
    assert d["Q1"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert d["Q1"]["reason"] == MISSING_DESIGN_REASON
    assert d["Q1"].get("terminal") is not True
    await rt2.ensure_lease(ttl_s=10_000)
    await _complete_report_and_finalize(rt2, tmp_path)
    assert rt2.view().phase == "closed"
    fin = _assert_terminated(await rt2.store.read_all())
    contrib = list(fin.payload.get("contributing_reasons") or [])
    assert "internal_error" in contrib
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_stamp_meta_missing_downgrades_needs_more_data(tmp_path, clock):
    """Fold sets artifact_kinds only after successful stamp — missing meta → not_attempted."""
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _RT(LocalProjectMixin):
        pass

    rt = _RT()
    rt.artifact_kinds = {"art_x": ARTIFACT_KIND_EXPERIMENT_DESIGN}  # pre-poison
    rt._stamp_meta_missing = []
    arts = __import__("coscientist.core.execution.artifacts", fromlist=["LocalArtifactStore"]).LocalArtifactStore(
        tmp_path / "arts"
    )
    rt.artifacts = arts
    # Soft stamp with missing meta must clear kinds and record tainted.
    assert rt._apply_kind_stamp_soft("art_x", ARTIFACT_KIND_EXPERIMENT_DESIGN) is False
    assert "art_x" not in rt.artifact_kinds
    assert rt._stamp_meta_missing and rt._stamp_meta_missing[0]["tainted"] is True
    assert (
        design_artifact_ok(
            artifact_id="art_x",
            artifact_lookup=arts.get_meta,
            journal_kinds={"art_x": ARTIFACT_KIND_EXPERIMENT_DESIGN},
        )
        is False
    )


def test_dispose_reject_detail_length_aligned():
    """``_note_reject`` and journaled QuestionDisposeRejected share one truncate length."""
    from datetime import datetime, timezone

    from coscientist.core.claims.dispose import (
        DISPOSE_REJECT_DETAIL_MAX,
        _note_reject,
        fold_question_dispose_rejected,
        question_dispose_rejected_event,
    )

    long = "x" * (DISPOSE_REJECT_DETAIL_MAX + 80)
    assert DISPOSE_REJECT_DETAIL_MAX == 500
    ev = question_dispose_rejected_event(
        study_id="st",
        detail=long,
        now=datetime.now(timezone.utc),
        question_id="Q1",
        disposition="needs_more_data",
    )
    assert len(str(ev.payload["detail"])) == DISPOSE_REJECT_DETAIL_MAX

    class _RT:
        _disposal_rejects: list = []

    rt = _RT()
    _note_reject(rt, long, report_line="line")
    assert len(rt._disposal_rejects) == 1
    assert len(rt._disposal_rejects[0]["detail"]) == DISPOSE_REJECT_DETAIL_MAX
    # Same truncated detail → same digest; second note is a no-op.
    _note_reject(rt, long, report_line="line")
    assert len(rt._disposal_rejects) == 1
    # Fold of the journaled payload matches live mirror length / digests.
    folded: list = []
    fold_question_dispose_rejected(folded, dict(ev.payload))
    assert len(folded) == 1
    assert folded[0]["detail"] == rt._disposal_rejects[0]["detail"]
    fold_question_dispose_rejected(folded, dict(ev.payload))
    assert len(folded) == 1
