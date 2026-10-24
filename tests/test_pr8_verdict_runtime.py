"""M1 PR8: verdict() runtime wiring, kind=verdict, claim carry-overs + review fixes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coscientist.core.claims.claims import (
    CLAIM_ATTR_WHITELIST,
    claims_missing_about,
    filtered_claims_by_target,
    runtime_claim_from_node,
)
from coscientist.core.claims.dispositions import dispositions
from coscientist.core.claims.graph import GraphEdge, GraphNode, GraphStore, default_execution_registry
from coscientist.core.claims.reasons import TaskReason
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.ids import claim_node_id, question_node_id
from coscientist.core.execution.domain import (
    HitlPolicy,
    InputSpec,
    QuestionSpec,
    RequirementRef,
    Study,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.execution.inputs import (
    InputUnavailable,
    bind_inputs,
    claim_nodes_from_attempt,
    collect_bindable_claims,
    verdict_binding_digest,
)
from tests.helpers import attach_executor, close_runtime, open_runtime
from tests.test_inputs import _task

_DERIVED_OK = {
    "criterion_status": "met",
    "evidence_kind": "computational",
    "frozen": True,
    "lit_own_criterion": False,
    "accepted": True,
    "artifact_id": "A1",
    # Core-stamped intent (never agent whitelist / never invent on read).
    "intent": "confirm",
    # Catalog-style SESOI threshold + core effect (exceeds sesoi).
    "sesoi": 0.5,
    "uses_sesoi": True,
    "used_sesoi": True,
    "predicted_direction": "positive",
    "effect": {
        "est": 1.5,
        "ci_low": 1.5,
        "ci_high": 1.5,
        "producer": "agent",
        "trusted": False,
        "core_evaluated": True,
        "countable": True,
        # Bound call_id required for countable (no {attempt}:derived invention).
        "produced_by_call_id": "PLACEHOLDER:1",
    },
    "check_role": "prediction",
    "refute_capable": True,
    "severe": True,
}


def _derived_for(attempt_id: str, **extra: object) -> dict:
    """``_DERIVED_OK`` with structural ``attempt_id`` + call_id on effect."""
    base: dict = {**_DERIVED_OK, **extra}
    eff = dict(base.get("effect") or {})
    extra_eff = extra.get("effect")
    if isinstance(extra_eff, dict):
        eff = {**eff, **extra_eff}
    eff["produced_by_call_id"] = f"{attempt_id}:1"
    eff["attempt_id"] = attempt_id
    base["effect"] = eff
    return base


def _apply_events(g: GraphStore, evs: list) -> None:
    for ev in evs:
        if ev.type == "GraphNodeUpserted":
            g.upsert_node(GraphNode.model_validate(ev.payload))
        elif ev.type == "GraphEdgeAdded":
            g.add_edge(GraphEdge.model_validate(ev.payload))


def _seed_question(g: GraphStore, sid: str, qid: str = "Q1") -> str:
    nid = question_node_id(sid, qid)
    g.upsert_node(
        GraphNode(
            node_id=nid,
            kind="question",
            study_id=sid,
            ref_id=qid,
            attrs={"formulation": "why"},
        )
    )
    return nid


def test_reupsert_keeps_worst_provenance():
    """Carry-over #1: stub claim cannot be laundered to real via same namespaced id."""
    g = GraphStore(default_execution_registry())
    sid = "st_launder"
    aid = f"{sid}/E0#1"
    _seed_question(g, sid)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    evs1, drops1 = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid,
        provenance="stub",
        claims=[{"id": "C1", "target": "Q1"}],
        now=now,
        graph=g,
        derived=_derived_for(aid),
    )
    assert drops1 == []
    _apply_events(g, evs1)
    cid = claim_node_id(sid, f"{aid}:C1")
    assert g.get_node(cid).attrs["provenance"] == "stub"
    evs2, _ = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid,
        provenance="real",
        claims=[{"id": "C1", "target": "Q1", "secret": "x", "criterion_status": "met"}],
        now=now,
        graph=g,
        derived=_derived_for(aid, accepted=True),
    )
    node_ev = next(e for e in evs2 if e.type == "GraphNodeUpserted" and e.entity_key == cid)
    assert node_ev.payload["attrs"]["provenance"] == "stub"
    assert "secret" not in node_ev.payload["attrs"]
    assert "criterion_status" in node_ev.payload["attrs"]  # core-derived
    assert node_ev.payload["attrs"]["criterion_status"] == "met"


def test_claim_attrs_whitelisted_no_counting_controls():
    """Agent cannot set criterion_status / frozen / intent / R17."""
    g = GraphStore(default_execution_registry())
    sid = "st_w"
    _seed_question(g, sid)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    aid = f"{sid}/E#1"
    evs, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid,
        provenance="real",
        claims=[
            {
                "id": "C1",
                "target": "Q1",
                "criterion_status": "met",
                "frozen": True,
                "intent": "refute",
                "confirming_evidence_ids": ["e1"],
                "stance": "supports",
                "evil": 1,
                "reason": "ok",
            }
        ],
        now=now,
        graph=g,
        derived=_derived_for(aid),
    )
    assert drops == []
    attrs = next(e for e in evs if e.type == "GraphNodeUpserted").payload["attrs"]
    assert "evil" not in attrs
    assert "stance" not in attrs
    # Agent intent=refute must not stick — core stamps confirm for questions.
    assert attrs.get("intent") == "confirm"
    # Agent criterion_status must not stick from whitelist — core derived overwrites.
    assert attrs["reason"] == "ok"
    assert attrs["target"] == "Q1"
    assert CLAIM_ATTR_WHITELIST == frozenset(
        {"target", "checked_against", "reason", "premise_idx"}
    )


def test_about_edge_emitted_unknown_target_dropped():
    g = GraphStore(default_execution_registry())
    sid = "st_about"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    aid = f"{sid}/E#1"
    evs, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid,
        provenance="real",
        claims=[{"id": "C1", "target": "Q1"}],
        now=now,
        graph=g,
        derived=_derived_for(aid),
    )
    assert drops and drops[0]["reason"] == "target_unknown"
    assert evs == []
    _seed_question(g, sid)
    evs2, drops2 = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid,
        provenance="real",
        claims=[{"id": "C1", "target": "Q1"}],
        now=now,
        graph=g,
        derived=_derived_for(aid),
    )
    assert drops2 == []
    _apply_events(g, evs2)
    cid = claim_node_id(sid, f"{sid}/E#1:C1")
    about = [e for e in g.edges_from(cid) if e.kind == "about"]
    assert len(about) == 1
    assert about[0].dst_id == question_node_id(sid, "Q1")
    assert claims_missing_about(g) == []


def test_garbage_attrs_dropped_not_raised():
    """Totality: confirming_evidence_ids=5 must not crash dispositions/finalize."""
    g = GraphStore(default_execution_registry())
    sid = "st_garbage"
    _seed_question(g, sid)
    cid = claim_node_id(sid, "orphan")
    g.upsert_node(
        GraphNode(
            node_id=cid,
            kind="claim",
            study_id=sid,
            ref_id=cid,
            attrs={
                "provenance": "real",
                "attempt_id": f"{sid}/E#1",
                "attempt_outcome": "SUCCESS",
                "target": "Q1",
                "criterion_status": "met",
                "accepted": True,
                "frozen": True,
                "evidence_kind": "computational",
                "confirming_evidence_ids": 5,  # garbage
            },
        )
    )
    g.add_edge(
        GraphEdge(
            edge_id=f"about:{cid}:{question_node_id(sid, 'Q1')}",
            kind="about",
            study_id=sid,
            src_id=cid,
            dst_id=question_node_id(sid, "Q1"),
        )
    )
    rc, reason = runtime_claim_from_node(
        g.get_node(cid), study_id=sid, graph=g, results={}
    )
    assert rc is None
    assert reason == "bad_confirming_evidence_ids"
    study = Study(
        study_id=sid,
        profile="research",
        request="q",
        catalog_version=3,
        hitl_policy=HitlPolicy(),
        deadline=datetime(2026, 1, 2, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    t = Task(
        task_id=f"{sid}/E1",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key="E1",
            title="e",
            goal="g",
            route="experiment",
            addresses=["Q1"],
        ),
        state=TaskState.DONE,
        outcome=TaskOutcome.SUCCESS,
        deadline=datetime(2026, 1, 1, tzinfo=UTC),
    )
    d = dispositions(
        tasks={t.task_id: t},
        plans={
            "pl": {
                "questions": [{"id": "Q1", "text": "why"}],
                "tasks": [{"task_key": "E1", "addresses": ["Q1"]}],
            }
        },
        graph=g,
        study=study,
        results={},
    )
    assert d["Q1"]["disposition"] == "not_attempted"


def test_foreign_claim_id_rejected():
    g = GraphStore(default_execution_registry())
    sid = "st_foreign"
    _seed_question(g, sid)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    aid = f"{sid}/E#1"
    foreign = claim_node_id(sid, f"{sid}/OTHER#1:C1")
    evs, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid,
        provenance="real",
        claims=[{"id": foreign, "target": "Q1"}],
        now=now,
        graph=g,
        derived=_derived_for(aid),
    )
    assert drops and drops[0]["reason"] == "foreign_claim_id"
    assert evs == []


def test_no_h_star_heuristic_auto_create():
    """H1 without an existing hypothesis node is unknown — dropped."""
    g = GraphStore(default_execution_registry())
    sid = "st_h"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    aid = f"{sid}/E#1"
    _, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid,
        provenance="real",
        claims=[{"id": "C1", "target": "H1"}],
        now=now,
        graph=g,
        derived=_derived_for(aid),
    )
    assert drops[0]["reason"] == "target_unknown"
    assert g.get_node(f"{sid}:hypothesis:H1") is None


@pytest.mark.asyncio
async def test_commit_failure_path_drops_claims(tmp_path, clock, runtime_cls):
    from unittest.mock import patch

    from coscientist.core.events.errors import CatalogError
    from coscientist.core.events.ids import claim_node_id as cid_fn
    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle, RequirementRef

    sid = "st_cfail"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    ex, arts = attach_executor(rt, tmp_path)
    assert ex.get_graph is not None
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    claim_id = cid_fn(sid, "cf")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="producer",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match")],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    assert rt.graph.get_node(question_node_id(sid, "Q1")) is not None
    aid0 = await rt.start_attempt("E0")
    content = b'{"count": 225}'
    art = arts.put(
        content, make_tool_ref(f"{aid0}:1", attempt_id=aid0), name="main", media_type="application/json"
    )

    def boom(*_a, **_k):
        raise CatalogError("forced")

    with patch("coscientist.core.execution.executor.compute_outcome", side_effect=boom):
        result = await ex.record(
            aid0,
            AgentTaskOutput(
                summary="fail",
                outputs={
                    "main": ArtifactHandle(artifact_id=art.artifact_id, name="main")
                },
                values={"count": 225},
                claims=[{"id": claim_id, "target": "Q1"}],
            ),
        )
    assert result.outcome.value == "FAILED"
    assert rt.graph.get_node(claim_id) is None
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_scientific_failed_keeps_claims(tmp_path, clock, runtime_cls):
    """Validity FAILED is still scientific — claim kept but not counted."""
    from coscientist.core.execution.domain import AgentTaskOutput

    sid = "st_fail_cl"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    ex, _arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="producer",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                # Explicit criterion (PlanValidator ignores catalog defaults).
                criteria=[RequirementRef(template_id="artifact_present")],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid0 = await rt.start_attempt("E0")
    # No artifact → artifact_present (validity) FAILED → scientific.
    result = await ex.record(
        aid0,
        AgentTaskOutput(
            summary="fail",
            outputs={},
            values={},
            claims=[{"id": "sci", "target": "Q1", "checked_against": "artifact_present"}],
        ),
    )
    assert result.outcome.value in ("FAILED", "PARTIAL")
    claims = list(rt.graph.nodes_by_kind("claim"))
    assert len(claims) == 1
    assert claims[0].attrs["criterion_status"] == "unassessed"
    assert claims[0].attrs["accepted"] is True
    assert claims[0].attrs["validity_ok"] is False
    assert claims[0].attrs["check_role"] == "validity"
    assert rt.results[aid0].get("result_class") == "scientific"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_infra_failed_producer_claim_not_bindable(tmp_path, clock, runtime_cls):
    """Infra FAILED (result_class=infra) claims are not bindable."""
    sid = "st_bind_fail"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    claim_id = claim_node_id(sid, "from_fail")
    aid_fake = f"{sid}/E0#1"
    await rt._append(
        [
            EventEnvelope(
                study_id=sid,
                type="GraphNodeUpserted",
                entity_key=claim_id,
                idem_key=make_idem("GraphNodeUpserted", claim_id),
                payload=GraphNode(
                    node_id=claim_id,
                    kind="claim",
                    study_id=sid,
                    ref_id="from_fail",
                    attrs={
                        "provenance": "real",
                        "attempt_id": aid_fake,
                        "attempt_outcome": "FAILED",
                        "target": "Q1",
                        "accepted": True,
                        "criterion_status": "met",
                        "frozen": True,
                        "evidence_kind": "computational",
                    },
                ).model_dump(mode="json"),
                ts=clock.now(),
            )
        ]
    )
    rt.results[aid_fake] = {
        "attempt_id": aid_fake,
        "outcome": "FAILED",
        "result_class": "infra",
        "reason": "catalog_error",
    }
    known, _ = collect_bindable_claims(rt.graph, results=rt.results)
    assert claim_id not in known
    child = _task("B", None, inputs=[InputSpec(kind="claim", ref=claim_id)])
    with pytest.raises(InputUnavailable, match="claim_unavailable"):
        bind_inputs(
            child,
            dep_tasks={},
            dep_outputs={},
            dep_provenance={},
            known_claim_ids=known,
        )
    await close_runtime(rt)


def test_open_runtime_requires_catalog():
    """Production factory has no minimal_test_catalog fallback."""
    import asyncio
    from pathlib import Path

    from coscientist.core.execution.runtime_factory import open_runtime as prod_open

    async def _run():
        with pytest.raises(TypeError, match="catalog"):
            await prod_open(Path("/tmp"), "st_no_cat", catalog=None)  # type: ignore[arg-type]

    asyncio.run(_run())


def test_bind_verdict_snapshot_digest():
    child = _task(
        "B",
        None,
        inputs=[InputSpec(kind="verdict", ref="Q1")],
    )
    snap = {
        "ref": "Q1",
        "verdict": "supported",
        "by_literature": False,
        "provenance": "real",
        "refutations_needed": 1,
        "refutation_priority": False,
    }
    with pytest.raises(InputUnavailable, match="verdict_unavailable"):
        bind_inputs(
            child,
            dep_tasks={},
            dep_outputs={},
            dep_provenance={},
            verdict_snapshots={},
        )
    bindings, prov = bind_inputs(
        child,
        dep_tasks={},
        dep_outputs={},
        dep_provenance={},
        verdict_snapshots={"Q1": snap},
    )
    assert bindings[0].kind == "verdict"
    assert bindings[0].digest == verdict_binding_digest(ref="Q1", snapshot=snap)
    assert prov == "real"


@pytest.mark.asyncio
async def test_accept_plan_allows_verdict_kind(tmp_path, clock, runtime_cls):
    sid = "st_verdict_ok"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                inputs=[InputSpec(kind="verdict", ref="Q1")],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_verdict_snapshot_persisted_on_bind(tmp_path, clock, runtime_cls):
    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle, RequirementRef

    sid = "st_v_bind"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid,
        finalize_reserve_s=5,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="producer",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E1",
                title="consumer",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                inputs=[InputSpec(kind="verdict", ref="Q1")],
                addresses=["Q1"],
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid0 = await rt.start_attempt("E0", provenance="real")
    content = b'{"count": 225}'
    art = arts.put(
        content, make_tool_ref(f"{aid0}:1", attempt_id=aid0), name="main", media_type="application/json"
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 225},
            claims=[{"id": "c1", "target": "Q1"}],
        ),
    )
    claims = list(rt.graph.nodes_by_kind("claim"))
    assert len(claims) == 1
    about = [e for e in rt.graph.edges_from(claims[0].node_id) if e.kind == "about"]
    assert len(about) == 1
    aid1 = await rt.start_attempt("E1")
    evs = await rt.store.read_all()
    started = next(
        e for e in evs if e.type == "AttemptStarted" and e.entity_key == aid1
    )
    assert started.payload["inputs"][0]["kind"] == "verdict"
    snap_ev = next(e for e in evs if e.type == "VerdictSnapshotsRecorded")
    assert "Q1" in snap_ev.payload["snapshots"]
    assert snap_ev.payload["scope"] == "bind"
    assert "Q1" in rt.verdict_snapshots
    await close_runtime(rt)


def test_dispositions_from_verdict_counting_claims():
    sid = "st_disp"
    g = GraphStore(default_execution_registry())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    _seed_question(g, sid)
    aid = f"{sid}/E1#1"
    evs, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid,
        provenance="real",
        claims=[{"id": "C1", "target": "Q1"}],
        now=now,
        graph=g,
        derived=_derived_for(aid),
    )
    assert drops == []
    _apply_events(g, evs)
    t = Task(
        task_id=f"{sid}/E1",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key="E1",
            title="e",
            goal="g",
            route="experiment",
            addresses=["Q1"],
        ),
        state=TaskState.DONE,
        outcome=TaskOutcome.SUCCESS,
        deadline=now,
    )
    study = Study(
        study_id=sid,
        profile="research",
        request="q",
        catalog_version=3,
        hitl_policy=HitlPolicy(),
        deadline=now + timedelta(seconds=60),
        created_at=now,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
    )
    plan = {
        "plan_id": "pl",
        "questions": [{"id": "Q1", "text": "why"}],
        "tasks": [{"task_key": "E1", "addresses": ["Q1"]}],
    }
    d = dispositions(
        tasks={t.task_id: t},
        plans={"pl": plan},
        graph=g,
        study=study,
        results={aid: {"outcome": "SUCCESS"}},
    )
    # PR8b: single severe confirm → survived (supported needs ≥2 + refute intent).
    assert d["Q1"]["disposition"] == "survived"
    assert d["Q1"]["verdict"] == "survived"
    # Same filter as snapshots (kind-prefixed keys)
    by = filtered_claims_by_target(g, study_id=sid, results={aid: {"outcome": "SUCCESS"}})
    assert "question:Q1" in by and by["question:Q1"][0].counts()


@pytest.mark.asyncio
async def test_runtime_disposition_answered_end_to_end(tmp_path, clock, runtime_cls):
    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle, RequirementRef

    sid = "st_ans"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid,
        finalize_reserve_s=5,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="producer",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match_sesoi")],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid0 = await rt.start_attempt("E0")
    content = b'{"effect": 2.0}'
    art = arts.put(
        content, make_tool_ref(f"{aid0}:1", attempt_id=aid0), name="main", media_type="application/json"
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"effect": 2.0},
            claims=[{"id": "ans", "target": "Q1"}],
        ),
    )
    d = dispositions(
        tasks=rt.tasks,
        plans=rt.plans,
        graph=rt.graph,
        study=rt.study,
        results=rt.results,
    )
    # Agent put + core SESOI eval → countable severe → survived (need 2 for supported).
    assert d["Q1"]["verdict"] == "survived"
    by = filtered_claims_by_target(
        rt.graph, study_id=sid, results=rt.results
    )
    assert by["question:Q1"][0].severe is True
    assert (by["question:Q1"][0].effect or {}).get("trusted") is False
    await close_runtime(rt)


def test_empty_provenance_list_is_stub():
    from coscientist.core.execution.domain import worst_provenance

    assert worst_provenance() == "stub"


def test_qh_logical_id_collision_does_not_merge():
    """Question and hypothesis with the same logical id stay separate."""
    from coscientist.core.claims.claims import compute_verdict_snapshots
    from coscientist.core.events.ids import hypothesis_node_id

    g = GraphStore(default_execution_registry())
    sid = "st_qh"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    _seed_question(g, sid, "X1")
    hid = hypothesis_node_id(sid, "X1")
    g.upsert_node(
        GraphNode(
            node_id=hid,
            kind="hypothesis",
            study_id=sid,
            ref_id="X1",
            attrs={"formulation": "h"},
        )
    )
    aid_q = f"{sid}/EQ#1"
    aid_h = f"{sid}/EH#1"
    evs_q, _ = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid_q,
        provenance="real",
        claims=[{"id": "cq", "target": "X1"}],
        now=now,
        graph=g,
        derived=_derived_for(aid_q),
    )
    _apply_events(g, evs_q)
    # Force about to hypothesis for second claim via full node id target.
    evs_h, drops_h = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid_h,
        provenance="real",
        claims=[{"id": "ch", "target": hid}],
        now=now,
        graph=g,
        derived=_derived_for(aid_h, criterion_status="not_met"),
    )
    assert drops_h == []
    _apply_events(g, evs_h)
    by = filtered_claims_by_target(g, study_id=sid)
    assert "question:X1" in by
    assert "hypothesis:X1" in by
    assert by["question:X1"][0].target_kind == "question"
    assert by["hypothesis:X1"][0].target_kind == "hypothesis"
    # Bind snapshot for bare "X1" uses question kind explicitly (not H's not_met).
    snaps = compute_verdict_snapshots(
        g, study_id=sid, any_experiment_done=True, refs=["X1"]
    )
    assert snaps["X1"]["target_kind"] == "question"
    assert snaps["X1"]["verdict"] == "survived"
    # Explicit hypothesis bind does not get the question verdict.
    snaps_h = compute_verdict_snapshots(
        g, study_id=sid, any_experiment_done=True, refs=["hypothesis:X1"]
    )
    assert snaps_h["X1"]["target_kind"] == "hypothesis"
    assert snaps_h["X1"]["verdict"] == "refuted"


def test_criterion_status_not_met_from_failed_check():
    """checked_against → FAILED ReqStatus ⇒ criterion_status=not_met."""
    from coscientist.core.execution.domain import CheckResult, ReqStatus, Task, TaskKind, TaskSpec
    from coscientist.core.execution.executor import _derive_claim_core_attrs

    now = datetime(2026, 1, 1, tzinfo=UTC)
    task = Task(
        task_id="st/E0",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key="E0",
            title="e",
            goal="g",
            route="experiment",
            criteria=[],
        ),
        state=TaskState.DONE,
        outcome=TaskOutcome.SUCCESS,
        deadline=now,
    )
    checks = [
        CheckResult(
            check_id="value_match#0",
            template_id="value_match",
            status=ReqStatus.FAILED,
            is_code=True,
            required=False,
            role="prediction",
            # Countable core-evaluated evidence required for inconsistent / not_met.
            effect={
                "est": 0.0,
                "kind": "point",
                "producer": "agent",
                "trusted": False,
                "core_evaluated": True,
                "countable": True,
            },
        )
    ]
    attrs = _derive_claim_core_attrs(
        task,
        TaskOutcome.SUCCESS,
        raw={"checked_against": "value_match", "target": "Q1"},
        checks=checks,
        graph=None,
        study_id="st",
        validated=True,
    )
    assert attrs["criterion_status"] == "not_met"
    assert attrs["accepted"] is True  # optional failure → stance, not rejection
    assert attrs["validity_ok"] is True
    assert attrs["check_role"] == "prediction"
    assert attrs["frozen"] is False  # no freeze node


def test_frozen_normalizes_check_id_to_template_id():
    """checked_against=check_id maps to criterion node keyed by template_id."""
    from coscientist.core.events.ids import criterion_node_id
    from coscientist.core.execution.domain import CheckResult, ReqStatus, Task, TaskKind, TaskSpec
    from coscientist.core.execution.executor import _derive_claim_core_attrs

    sid = "st_frz"
    g = GraphStore(default_execution_registry())
    cnid = criterion_node_id(sid, "value_match")
    g.upsert_node(
        GraphNode(
            node_id=cnid,
            kind="criterion",
            study_id=sid,
            ref_id="value_match",
            attrs={"frozen": True, "frozen_at_seq": 3, "template_id": "value_match"},
        )
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    task = Task(
        task_id=f"{sid}/E0",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key="E0",
            title="e",
            goal="g",
            route="experiment",
            criteria=[],
        ),
        state=TaskState.DONE,
        outcome=TaskOutcome.SUCCESS,
        deadline=now,
    )
    checks = [
        CheckResult(
            check_id="value_match#0",
            template_id="value_match",
            status=ReqStatus.MET,
            is_code=True,
            required=True,
            role="prediction",
        )
    ]
    attrs = _derive_claim_core_attrs(
        task,
        TaskOutcome.SUCCESS,
        raw={"checked_against": "value_match#0", "target": "Q1"},
        checks=checks,
        graph=g,
        study_id=sid,
        validated=True,
    )
    assert attrs["frozen"] is True
    assert attrs["criterion_status"] == "met"


def test_criterion_match_is_exact_not_endswith():
    """C11 must not match template_id ``1`` via endswith."""
    from coscientist.core.execution.domain import CheckResult, ReqStatus, Task, TaskKind, TaskSpec
    from coscientist.core.execution.executor import _derive_claim_core_attrs

    now = datetime(2026, 1, 1, tzinfo=UTC)
    task = Task(
        task_id="st/E0",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key="E0", title="e", goal="g", route="experiment", criteria=[]
        ),
        state=TaskState.DONE,
        outcome=TaskOutcome.SUCCESS,
        deadline=now,
    )
    checks = [
        CheckResult(
            check_id="1#0",
            template_id="1",
            status=ReqStatus.MET,
            is_code=True,
            required=True,
            role="unknown",
        )
    ]
    attrs = _derive_claim_core_attrs(
        task,
        TaskOutcome.SUCCESS,
        raw={"checked_against": "C11", "target": "Q1"},
        checks=checks,
        graph=None,
        study_id="st",
        validated=True,
    )
    assert attrs["criterion_status"] == "unassessed"


@pytest.mark.asyncio
async def test_runtime_refuted_required_check_failed(tmp_path, clock, runtime_cls):
    """(a) Failed required prediction check → scientific, not_met, refuted e2e.

    accepted stays True (result recorded + structurally valid). Outcome may be
    FAILED/PARTIAL; claims are kept (not infra). graph_schema invariant 10.
    """
    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle, RequirementRef

    sid = "st_ref"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    assert rt.executor is not None and rt.executor.get_graph is not None
    arts = rt.artifacts
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid,
        finalize_reserve_s=5,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="falsify",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[
                    RequirementRef(template_id="value_match", required=True),
                ],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    assert any(n.kind == "criterion" for n in rt.graph.nodes_by_kind("criterion"))
    aid0 = await rt.start_attempt("E0", provenance="real")
    # value_match required, target 225 — wrong count ⇒ FAILED/PARTIAL scientific.
    # Production path: agent put + core catalog evaluation → countable refute.
    art = arts.put(
        b'{"count": 999}',
        make_tool_ref(f"{aid0}:1", attempt_id=aid0),
        name="main",
        media_type="application/json",
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="falsified",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 999},
            claims=[
                {
                    "id": "ref",
                    "target": "Q1",
                    "checked_against": "value_match",
                }
            ],
        ),
    )
    claim = next(iter(rt.graph.nodes_by_kind("claim")))
    assert claim.attrs["criterion_status"] == "not_met"
    assert claim.attrs["accepted"] is True
    assert claim.attrs["validity_ok"] is True
    assert claim.attrs["check_role"] == "prediction"
    assert claim.attrs["frozen"] is True
    assert claim.attrs["attempt_outcome"] in ("FAILED", "PARTIAL")
    assert (claim.attrs.get("effect") or {}).get("countable") is True
    assert (claim.attrs.get("effect") or {}).get("trusted") is False
    res = rt.results[aid0]
    assert res.get("result_class") == "scientific"
    d = dispositions(
        tasks=rt.tasks,
        plans=rt.plans,
        graph=rt.graph,
        study=rt.study,
        results=rt.results,
    )
    assert d["Q1"]["disposition"] == "refuted"
    assert d["Q1"]["verdict"] == "refuted"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_validity_failed_claim_invalid_inconclusive(
    tmp_path, clock, runtime_cls
):
    """(b) Failed required validity check → invalid claim, inconclusive + excluded."""
    from coscientist.core.execution.domain import AgentTaskOutput, RequirementRef

    sid = "st_val"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid,
        finalize_reserve_s=5,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="validity",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[
                    RequirementRef(template_id="artifact_present", required=True),
                ],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid0 = await rt.start_attempt("E0", provenance="real")
    # No artifact → artifact_present FAILED (validity).
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="broken aux",
            outputs={},
            values={},
            claims=[
                {
                    "id": "c",
                    "target": "Q1",
                    "checked_against": "artifact_present",
                }
            ],
        ),
    )
    claim = next(iter(rt.graph.nodes_by_kind("claim")))
    assert claim.attrs["accepted"] is True
    assert claim.attrs["validity_ok"] is False
    assert claim.attrs["criterion_status"] == "unassessed"
    assert claim.attrs["check_role"] == "validity"
    assert claim.attrs.get("exclusion_reason") == "validity_failed"
    assert rt.results[aid0].get("result_class") == "scientific"
    d = dispositions(
        tasks=rt.tasks,
        plans=rt.plans,
        graph=rt.graph,
        study=rt.study,
        results=rt.results,
    )
    assert d["Q1"]["disposition"] == "inconclusive"
    assert d["Q1"]["verdict"] == "inconclusive"
    assert d["Q1"]["reason"] == "validity_failed"
    assert d["Q1"]["excluded_claims"]
    assert d["Q1"]["excluded_claims"][0]["reason"] == "validity_failed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_plan_rejects_role_disagreeing_with_catalog(tmp_path, clock, runtime_cls):
    """Plan claiming validity for a prediction template is rejected."""
    sid = "st_role_rej"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    pid = await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="dodge",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[
                    RequirementRef(
                        template_id="value_match",
                        required=True,
                        role="validity",  # catalog says prediction
                    ),
                ],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    assert pid == ""
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "PlanRefused"]
    assert refused and "disagrees with template" in str(refused[0].payload.get("detail") or "")
    assert any(e.type == "StudyFinalized" for e in events)
    await close_runtime(rt)

    sid2 = "st_role_rej2"
    rt2 = await open_runtime(tmp_path, sid2, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt2.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid2,
        finalize_reserve_s=5,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt2.start_attempt(key)
        await rt2.cancel_task(key, reason="cancelled")
    pid2 = await rt2.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="unknown",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[
                    RequirementRef(template_id="no_such_template", required=True),
                ],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    assert pid2 == ""
    events2 = await rt2.store.read_all()
    refused2 = [e for e in events2 if e.type == "PlanRefused"]
    assert refused2 and "unknown criterion template" in str(refused2[0].payload.get("detail") or "")
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_scientific_failed_refute_no_retry_no_hc_cascade(
    tmp_path, clock, runtime_cls
):
    """Scientific FAILED refute: no retry, no H-c cascade, budget ok, finalize refuted."""
    from decimal import Decimal

    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.domain import (
        AgentTaskOutput,
        ArtifactHandle,
        Budget,
        RequirementRef,
    )

    sid = "st_sci_hc"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    arts = rt.artifacts
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
        budget=Budget(max_llm_calls=50, max_cost_rub=Decimal("100")),
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E_ref",
                title="falsify",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match", required=True)],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E_child",
                title="depends",
                goal="g",
                route="experiment",
                depends_on=["E_ref"],
                criteria=[],
                addresses=["Q1"],
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid = await rt.start_attempt("E_ref", provenance="real")
    await rt.record_budget_debit(spent_rub=Decimal("1.00"), request_id="debit-1")
    art = arts.put(
        b'{"count": 999}',
        make_tool_ref(f"{aid}:1", attempt_id=aid),
        name="main",
        media_type="application/json",
    )
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="falsified",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 999},
            claims=[
                {"id": "ref", "target": "Q1", "checked_against": "value_match"},
            ],
        ),
    )
    parent = rt.tasks[f"{sid}/E_ref"]
    assert parent.state == TaskState.DONE
    assert parent.outcome == TaskOutcome.FAILED
    assert rt.results[aid].get("result_class") == "scientific"
    assert len(parent.attempts) == 1

    await rt.tick()
    # After tick: still no retry attempt / retry_at for the scientific FAILED task.
    assert len(parent.attempts) == 1
    assert f"{sid}/E_ref" not in getattr(rt, "_retry_at", {})
    child = rt.tasks[f"{sid}/E_child"]
    # Scientific FAILED must not H-c cascade-cancel the dependent.
    assert child.state != TaskState.DONE or child.outcome != TaskOutcome.SKIPPED
    assert child.reason != TaskReason.INPUT_UNAVAILABLE

    # Finish child + remaining stages so study can finalize.
    await rt.start_attempt("E_child")
    await rt.cancel_task("E_child", reason="cancelled")
    await rt.start_attempt("REPORT")
    await rt.cancel_task("REPORT", reason="cancelled")
    await rt.tick()
    assert rt._finalized
    events = await rt.store.read_all()
    debits = [e for e in events if e.type == "BudgetDebited"]
    assert len(debits) == 1
    assert Decimal(str(debits[0].payload["spent_rub"])) == Decimal("1.00")
    assert debits[0].payload["request_id"] == "debit-1"
    fin = next(e for e in events if e.type == "StudyFinalized")
    assert fin.payload["question_dispositions"]["Q1"]["verdict"] == "refuted"
    await close_runtime(rt)


def test_validity_exclusion_does_not_cap_when_valid_claims_remain():
    """3 valid real confirmations + 1 validity-failed → normal supported, not capped."""
    from coscientist.core.claims.claims import (
        RuntimeClaim,
        compute_verdict_snapshots,
        excluded_claims_report,
    )
    from coscientist.core.claims.rules import verdict
    from coscientist.core.execution.domain import Study

    valid = [
        RuntimeClaim(
            criterion_status="met",
            accepted=True,
            provenance="real",
            frozen=True,
            attempt_id=f"S{i}",
            artifact_id=f"A{i}",
            validity_ok=True,
            check_role="prediction",
            target="Q1",
            sesoi=0.5,
            uses_sesoi=True,
            used_sesoi=True,
            predicted_direction="positive",
            effect={
                "est": 1.5,
                "ci_low": 1.5,
                "ci_high": 1.5,
                "producer": "agent",
                "trusted": False,
                "core_evaluated": True,
                "countable": True,
            },
            claim_outcome="consistent",
            severe=True,
        )
        for i in range(3)
    ]
    bad = RuntimeClaim(
        criterion_status="unassessed",
        accepted=True,
        provenance="real",
        frozen=True,
        attempt_id="V0",
        artifact_id="AV",
        validity_ok=False,
        check_role="validity",
        exclusion_reason="validity_failed",
        target="Q1",
    )
    claims = [*valid, bad]
    # PR8b: three confirms without refute intent → survived; invalid excluded.
    assert verdict(claims, True, k=3)[0] == "survived"
    excl = excluded_claims_report(claims)
    assert len(excl) == 1 and excl[0]["reason"] == "validity_failed"
    # Snapshot path: same rule — no force to inconclusive.
    g = GraphStore(default_execution_registry())
    sid = "st_mix"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    _seed_question(g, sid, "Q1")
    for i in range(len(valid)):
        aid = f"{sid}/E{i}#1"
        evs, drops = claim_nodes_from_attempt(
            study_id=sid,
            attempt_id=aid,
            provenance="real",
            claims=[{"id": f"c{i}", "target": "Q1"}],
            now=now,
            graph=g,
            derived=_derived_for(
                aid,
                criterion_status="met",
                validity_ok=True,
                check_role="prediction",
                artifact_id=f"A{i}",
            ),
        )
        assert drops == []
        _apply_events(g, evs)
    aid_b = f"{sid}/Ev#1"
    evs_b, drops_b = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid_b,
        provenance="real",
        claims=[{"id": "bad", "target": "Q1"}],
        now=now,
        graph=g,
        derived=_derived_for(
            aid_b,
            criterion_status="unassessed",
            validity_ok=False,
            check_role="validity",
            exclusion_reason="validity_failed",
            artifact_id="AV",
        ),
    )
    assert drops_b == []
    _apply_events(g, evs_b)
    study = Study(
        study_id=sid,
        profile="research",
        request="q",
        catalog_version=3,
        hitl_policy=HitlPolicy(),
        deadline=now + timedelta(seconds=60),
        created_at=now,
        confirmations_needed=3,
    )
    snaps = compute_verdict_snapshots(
        g,
        study_id=sid,
        study=study,
        any_experiment_done=True,
        refs=["Q1"],
    )
    assert snaps["Q1"]["verdict"] == "survived"
    assert snaps["Q1"]["excluded_claims"][0]["reason"] == "validity_failed"
    assert snaps["Q1"].get("reason") != "validity_failed"


def test_unknown_role_exclusion_never_forces_verdict():
    """unknown_role is listed but does not force inconclusive by itself."""
    from coscientist.core.claims.claims import (
        RuntimeClaim,
        compute_verdict_snapshots,
        excluded_claims_report,
    )
    from coscientist.core.execution.domain import Study

    bad = RuntimeClaim(
        criterion_status="unassessed",
        accepted=True,
        provenance="real",
        frozen=True,
        attempt_id="U0",
        artifact_id="AU",
        validity_ok=False,
        check_role="unknown",
        exclusion_reason="unknown_role",
        target="Q1",
        node_id="n1",
    )
    excl = excluded_claims_report([bad])
    assert excl == [
        {"attempt_id": "U0", "node_id": "n1", "reason": "unknown_role"}
    ]

    # Snapshot: unknown_role alone → untested (never forces inconclusive).
    g = GraphStore(default_execution_registry())
    sid = "st_unk"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    _seed_question(g, sid, "Q1")
    aid = f"{sid}/E0#1"
    evs, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid,
        provenance="real",
        claims=[{"id": "u", "target": "Q1"}],
        now=now,
        graph=g,
        derived=_derived_for(
            aid,
            criterion_status="unassessed",
            validity_ok=False,
            check_role="unknown",
            exclusion_reason="unknown_role",
            artifact_id="AU",
        ),
    )
    assert drops == []
    _apply_events(g, evs)
    study = Study(
        study_id=sid,
        profile="research",
        request="q",
        catalog_version=3,
        hitl_policy=HitlPolicy(),
        deadline=now + timedelta(seconds=60),
        created_at=now,
        confirmations_needed=1,
    )
    snaps = compute_verdict_snapshots(
        g,
        study_id=sid,
        study=study,
        any_experiment_done=True,
        refs=["Q1"],
    )
    # Only-excluded claims → inconclusive (not untested).
    assert snaps["Q1"]["verdict"] == "inconclusive"
    assert snaps["Q1"]["excluded_claims"][0]["reason"] == "unknown_role"
    assert snaps["Q1"].get("reason") != "validity_failed"


def test_validity_only_evidence_inconclusive():
    """A validity failure as the only evidence → inconclusive (validity_failed)."""
    from coscientist.core.claims.claims import RuntimeClaim, compute_verdict_snapshots
    from coscientist.core.execution.domain import Study

    g = GraphStore(default_execution_registry())
    sid = "st_val_only"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    _seed_question(g, sid, "Q1")
    aid = f"{sid}/E0#1"
    evs, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid,
        provenance="real",
        claims=[{"id": "bad", "target": "Q1"}],
        now=now,
        graph=g,
        derived=_derived_for(
            aid,
            criterion_status="unassessed",
            validity_ok=False,
            check_role="validity",
            exclusion_reason="validity_failed",
            artifact_id="AV",
        ),
    )
    assert drops == []
    _apply_events(g, evs)
    study = Study(
        study_id=sid,
        profile="research",
        request="q",
        catalog_version=3,
        hitl_policy=HitlPolicy(),
        deadline=now + timedelta(seconds=60),
        created_at=now,
        confirmations_needed=1,
    )
    snaps = compute_verdict_snapshots(
        g,
        study_id=sid,
        study=study,
        any_experiment_done=True,
        refs=["Q1"],
    )
    assert snaps["Q1"]["verdict"] == "inconclusive"
    assert snaps["Q1"]["reason"] == "validity_failed"
    assert snaps["Q1"]["excluded_claims"][0]["reason"] == "validity_failed"
    # Sanity: RuntimeClaim path agrees.
    only = RuntimeClaim(
        criterion_status="unassessed",
        accepted=True,
        provenance="real",
        frozen=True,
        attempt_id="V0",
        artifact_id="AV",
        validity_ok=False,
        check_role="validity",
        exclusion_reason="validity_failed",
        target="Q1",
    )
    assert not only.counts()


@pytest.mark.asyncio
async def test_open_runtime_wires_executor_get_graph(tmp_path, clock, runtime_cls):
    """open_runtime (core + experimental) builds Executor via make_executor — no manual call."""
    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle, RequirementRef
    from tests.helpers import open_runtime_experimental

    for factory, sid in (
        (open_runtime, "st_core_fac"),
        (open_runtime_experimental, "st_exp_fac"),
    ):
        rt = await factory(tmp_path, sid, clock=clock, owner="t1")
        assert rt.executor is not None
        assert rt.executor.get_graph is not None
        assert rt.executor.get_graph() is rt.graph
        assert rt.artifacts is not None
        await rt.create_study(
            profile="research",
            request="q",
            catalog_version=3,
            deadline=clock.now() + timedelta(seconds=180),
            sid=sid,
            finalize_reserve_s=5,
        )
        for key in ("SPEC", "HYP", "LIT"):
            await rt.start_attempt(key)
            await rt.cancel_task(key, reason="cancelled")
        await rt.accept_plan(
            [
                TaskSpec(
                    task_key="E0",
                    title="producer",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    criteria=[RequirementRef(template_id="value_match")],
                    addresses=["Q1"],
                )
            ],
            questions=[QuestionSpec(id="Q1", text="why")],
        )
        aid0 = await rt.start_attempt("E0")
        art = rt.artifacts.put(
            b'{"count": 225}',
            make_tool_ref(f"{aid0}:1", attempt_id=aid0),
            name="main",
            media_type="application/json",
        )
        await rt.executor.record(
            aid0,
            AgentTaskOutput(
                summary="ok",
                outputs={
                    "main": ArtifactHandle(artifact_id=art.artifact_id, name="main")
                },
                values={"count": 225},
                claims=[{"id": "c1", "target": "Q1"}],
            ),
        )
        claims = list(rt.graph.nodes_by_kind("claim"))
        assert len(claims) == 1
        about = [e for e in rt.graph.edges_from(claims[0].node_id) if e.kind == "about"]
        assert len(about) == 1
        await close_runtime(rt)


def test_priority_refuted_stub_support_note_in_report_payload():
    """Priority-win refuted + stub supports → note on hypothesis_verdicts snapshot."""
    from coscientist.core.claims.claims import compute_verdict_snapshots, hypothesis_verdicts
    from coscientist.core.events.ids import hypothesis_node_id

    g = GraphStore(default_execution_registry())
    sid = "st_prio_note"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    hid = hypothesis_node_id(sid, "H1")
    g.upsert_node(
        GraphNode(
            node_id=hid,
            kind="hypothesis",
            study_id=sid,
            ref_id="H1",
            attrs={"formulation": "h"},
        )
    )
    aid_sup = f"{sid}/E_sup#1"
    aid_ref = f"{sid}/E_ref#1"
    evs_sup, drops_sup = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid_sup,
        provenance="stub",
        claims=[{"id": "sup", "target": "H1"}],
        now=now,
        graph=g,
        derived=_derived_for(aid_sup, criterion_status="met"),
    )
    assert drops_sup == []
    _apply_events(g, evs_sup)
    evs_ref, drops_ref = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid_ref,
        provenance="real",
        claims=[{"id": "ref", "target": "H1"}],
        now=now,
        graph=g,
        derived=_derived_for(aid_ref, criterion_status="not_met"),
    )
    assert drops_ref == []
    _apply_events(g, evs_ref)
    study = Study(
        study_id=sid,
        profile="research",
        request="q",
        catalog_version=3,
        hitl_policy=HitlPolicy(),
        deadline=now + timedelta(seconds=60),
        created_at=now,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=True,
    )
    results = {
        aid_sup: {"outcome": "SUCCESS"},
        aid_ref: {"outcome": "SUCCESS"},
    }
    snaps = compute_verdict_snapshots(
        g,
        study_id=sid,
        study=study,
        results=results,
        any_experiment_done=True,
        refs=["hypothesis:H1"],
    )
    assert snaps["H1"]["verdict"] == "refuted"
    assert snaps["H1"]["note"] == "refuted; 1 supporting results were stub/partial"
    hyp = hypothesis_verdicts(
        g,
        study_id=sid,
        study=study,
        results=results,
        any_experiment_done=True,
    )
    assert hyp["H1"]["verdict"] == "refuted"
    assert hyp["H1"]["note"] == "refuted; 1 supporting results were stub/partial"
    # Without priority: stub excluded → only real ref → still refuted + note.
    study_off = study.model_copy(update={"refutation_priority": False})
    snaps_off = compute_verdict_snapshots(
        g,
        study_id=sid,
        study=study_off,
        results=results,
        any_experiment_done=True,
        refs=["hypothesis:H1"],
    )
    assert snaps_off["H1"]["verdict"] == "refuted"
    assert snaps_off["H1"]["note"] == "refuted; 1 supporting results were stub/partial"


def test_intent_from_producing_experiment_two_opposite():
    """Two experiments, opposite tests.intent → claims pick their producer."""
    from coscientist.core.claims.claims import intent_from_producing_experiment, runtime_claim_from_node
    from coscientist.core.events.ids import experiment_node_id, hypothesis_node_id

    g = GraphStore(default_execution_registry())
    sid = "st_dual"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    hid = hypothesis_node_id(sid, "H1")
    g.upsert_node(
        GraphNode(
            node_id=hid, kind="hypothesis", study_id=sid, ref_id="H1", attrs={}
        )
    )
    for key, intent in (("E_confirm", "confirm"), ("E_refute", "refute")):
        eid = experiment_node_id(sid, key)
        g.upsert_node(
            GraphNode(
                node_id=eid,
                kind="experiment",
                study_id=sid,
                ref_id=key,
                attrs={"task_key": key},
            )
        )
        g.add_edge(
            GraphEdge(
                edge_id=f"tests:{eid}:{hid}",
                kind="tests",
                study_id=sid,
                src_id=eid,
                dst_id=hid,
                attrs={"intent": intent},
                created_at=now,
            )
        )
    aid_c = f"{sid}/E_confirm#1"
    aid_r = f"{sid}/E_refute#1"
    assert (
        intent_from_producing_experiment(
            g,
            study_id=sid,
            attempt_id=aid_c,
            target_logical="H1",
            target_kind="hypothesis",
        )
        == "confirm"
    )
    assert (
        intent_from_producing_experiment(
            g,
            study_id=sid,
            attempt_id=aid_r,
            target_logical="H1",
            target_kind="hypothesis",
        )
        == "refute"
    )
    for aid, want in ((aid_c, "confirm"), (aid_r, "refute")):
        evs, drops = claim_nodes_from_attempt(
            study_id=sid,
            attempt_id=aid,
            provenance="real",
            claims=[{"id": "c", "target": "H1"}],
            now=now,
            graph=g,
            derived=_derived_for(aid),
        )
        assert drops == []
        _apply_events(g, evs)
        node = g.get_node(claim_node_id(sid, f"{aid}:c"))
        rc, reason = runtime_claim_from_node(node, study_id=sid, graph=g)
        assert reason is None and rc is not None
        assert rc.intent == want


@pytest.mark.asyncio
async def test_verdict_snapshots_replay_and_invalidation(tmp_path, clock, runtime_cls):
    """VerdictSnapshotsRecorded: record → reopen → read; claim invalidates."""
    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle, RequirementRef

    sid = "st_snap_replay"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid,
        finalize_reserve_s=5,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="producer",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            ),
            TaskSpec(
                task_key="E1",
                title="consumer",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                inputs=[InputSpec(kind="verdict", ref="Q1")],
                addresses=["Q1"],
            ),
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid0 = await rt.start_attempt("E0", provenance="real")
    art = arts.put(
        b'{"count": 225}',
        make_tool_ref(f"{aid0}:1", attempt_id=aid0),
        name="main",
        media_type="application/json",
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 225},
            claims=[{"id": "c1", "target": "Q1"}],
        ),
    )
    await rt.start_attempt("E1")
    assert "Q1" in rt.verdict_snapshots
    snap_before = dict(rt.verdict_snapshots["Q1"])
    await close_runtime(rt)

    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    assert "Q1" in rt2.verdict_snapshots
    assert rt2.verdict_snapshots["Q1"]["verdict"] == snap_before["verdict"]

    # New claim about Q1 invalidates only that target.
    rt2.verdict_snapshots["OTHER"] = {"verdict": "untested", "k": 1}
    claim_nodes = list(rt2.graph.nodes_by_kind("claim"))
    assert claim_nodes
    rt2._invalidate_verdict_snapshots_for_target("Q1")
    assert "Q1" not in rt2.verdict_snapshots
    assert "OTHER" in rt2.verdict_snapshots
    await close_runtime(rt2)


def test_hypothesis_verdicts_not_in_bind_snapshot_cache():
    """_fold_finalized_graph keeps hypothesis_verdicts off the bind cache."""
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _T(LocalProjectMixin):
        def __init__(self) -> None:
            self.graph = GraphStore(default_execution_registry())
            self.verdict_snapshots = {"Q_bind": {"verdict": "supported", "k": 1}}
            self.hypothesis_verdicts = {}

    t = _T()
    t._fold_finalized_graph(
        "st_hyp",
        {
            "question_dispositions": {},
            "hypothesis_verdicts": {"H1": {"verdict": "survived", "k": 1}},
            "terminal_reason": "completed",
        },
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert "Q_bind" in t.verdict_snapshots
    assert "H1" not in t.verdict_snapshots
    assert t.hypothesis_verdicts.get("H1", {}).get("verdict") == "survived"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="R17 runtime metadata not wired in M1 PR8 (DESIGN_M1 §12)",
)
@pytest.mark.asyncio
async def test_r17_runtime_metadata_not_wired_yet(tmp_path, clock, runtime_cls):
    """xfail: Executor does not yet stamp criterion_frozen_at_seq on claims."""
    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle, RequirementRef

    sid = "st_r17"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=180),
        sid=sid,
        finalize_reserve_s=5,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="producer",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="value_match")],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid0 = await rt.start_attempt("E0")
    art = arts.put(
        b'{"count": 225}',
        make_tool_ref(f"{aid0}:1", attempt_id=aid0),
        name="main",
        media_type="application/json",
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 225},
            claims=[{"id": "c1", "target": "Q1", "checked_against": "value_match"}],
        ),
    )
    claim = next(iter(rt.graph.nodes_by_kind("claim")))
    # Will XPASS once R17 metadata is wired; until then AssertionError → xfail.
    assert claim.attrs.get("criterion_frozen_at_seq") is not None
    await close_runtime(rt)
