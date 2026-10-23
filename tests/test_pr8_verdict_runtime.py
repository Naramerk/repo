"""M1 PR8: verdict() runtime wiring, kind=verdict, claim carry-overs + review fixes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coscientist.core.claims import (
    CLAIM_ATTR_WHITELIST,
    claims_missing_about,
    filtered_claims_by_target,
    runtime_claim_from_node,
)
from coscientist.core.dispositions import dispositions
from coscientist.core.domain import (
    HitlPolicy,
    InputSpec,
    QuestionSpec,
    Study,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.graph import GraphEdge, GraphNode, GraphStore, default_execution_registry
from coscientist.core.ids import claim_node_id, question_node_id
from coscientist.core.inputs import (
    InputUnavailable,
    bind_inputs,
    claim_nodes_from_attempt,
    collect_bindable_claims,
    verdict_binding_digest,
)
from coscientist.core.runtime_factory import close_runtime, open_runtime
from tests.helpers import attach_executor
from tests.test_inputs import _task

_DERIVED_OK = {
    "criterion_status": "met",
    "evidence_kind": "computational",
    "frozen": True,
    "lit_own_criterion": False,
    "accepted": True,
    "artifact_id": "A1",
}


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
        derived=_DERIVED_OK,
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
        derived={**_DERIVED_OK, "accepted": True},
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
    evs, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=f"{sid}/E#1",
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
        derived=_DERIVED_OK,
    )
    assert drops == []
    attrs = next(e for e in evs if e.type == "GraphNodeUpserted").payload["attrs"]
    assert "evil" not in attrs
    assert "stance" not in attrs
    assert "intent" not in attrs or attrs.get("intent") is None
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
    evs, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=f"{sid}/E#1",
        provenance="real",
        claims=[{"id": "C1", "target": "Q1"}],
        now=now,
        graph=g,
        derived=_DERIVED_OK,
    )
    assert drops and drops[0]["reason"] == "target_unknown"
    assert evs == []
    _seed_question(g, sid)
    evs2, drops2 = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=f"{sid}/E#1",
        provenance="real",
        claims=[{"id": "C1", "target": "Q1"}],
        now=now,
        graph=g,
        derived=_DERIVED_OK,
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
        catalog_version="0.1.0",
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
    foreign = claim_node_id(sid, f"{sid}/OTHER#1:C1")
    evs, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=f"{sid}/E#1",
        provenance="real",
        claims=[{"id": foreign, "target": "Q1"}],
        now=now,
        graph=g,
        derived=_DERIVED_OK,
    )
    assert drops and drops[0]["reason"] == "foreign_claim_id"
    assert evs == []


def test_no_h_star_heuristic_auto_create():
    """H1 without an existing hypothesis node is unknown — dropped."""
    g = GraphStore(default_execution_registry())
    sid = "st_h"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    _, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=f"{sid}/E#1",
        provenance="real",
        claims=[{"id": "C1", "target": "H1"}],
        now=now,
        graph=g,
        derived=_DERIVED_OK,
    )
    assert drops[0]["reason"] == "target_unknown"
    assert g.get_node(f"{sid}:hypothesis:H1") is None


@pytest.mark.asyncio
async def test_commit_failure_path_drops_claims(tmp_path, clock, runtime_cls):
    from unittest.mock import patch

    from coscientist.core.artifacts import make_tool_ref
    from coscientist.core.domain import AgentTaskOutput, ArtifactHandle, RequirementRef
    from coscientist.core.errors import CatalogError
    from coscientist.core.ids import claim_node_id as cid_fn

    sid = "st_cfail"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
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
        content, make_tool_ref(f"{aid0}:1"), name="main", media_type="application/json"
    )

    def boom(*_a, **_k):
        raise CatalogError("forced")

    with patch("coscientist.core.executor.compute_outcome", side_effect=boom):
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
async def test_failed_attempt_claims_not_emitted(tmp_path, clock, runtime_cls):
    from coscientist.core.domain import AgentTaskOutput

    sid = "st_fail_cl"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
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
                criteria=[],
                addresses=["Q1"],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid0 = await rt.start_attempt("E0")
    result = await ex.record(
        aid0,
        AgentTaskOutput(
            summary="fail",
            outputs={},
            values={},
            claims=[{"id": "bad", "target": "Q1"}],
        ),
    )
    assert result.outcome.value == "FAILED"
    assert list(rt.graph.nodes_by_kind("claim")) == []
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_failed_producer_claim_not_bindable(tmp_path, clock, runtime_cls):
    sid = "st_bind_fail"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
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
                    },
                ).model_dump(mode="json"),
                ts=clock.now(),
            )
        ]
    )
    rt.results[aid_fake] = {"attempt_id": aid_fake, "outcome": "FAILED"}
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
        catalog_version="0.1.0",
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
    from coscientist.core.artifacts import make_tool_ref
    from coscientist.core.domain import AgentTaskOutput, ArtifactHandle, RequirementRef

    sid = "st_v_bind"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
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
        content, make_tool_ref(f"{aid0}:1"), name="main", media_type="application/json"
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
        derived=_DERIVED_OK,
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
        catalog_version="0.1.0",
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
    assert d["Q1"]["disposition"] == "answered"
    assert d["Q1"]["verdict"] == "supported"
    # Same filter as snapshots
    by = filtered_claims_by_target(g, study_id=sid, results={aid: {"outcome": "SUCCESS"}})
    assert "Q1" in by and by["Q1"][0].counts()


@pytest.mark.asyncio
async def test_runtime_disposition_answered_end_to_end(tmp_path, clock, runtime_cls):
    from coscientist.core.artifacts import make_tool_ref
    from coscientist.core.domain import AgentTaskOutput, ArtifactHandle, RequirementRef

    sid = "st_ans"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
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
    content = b'{"count": 225}'
    art = arts.put(
        content, make_tool_ref(f"{aid0}:1"), name="main", media_type="application/json"
    )
    await rt.executor.record(
        aid0,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 225},
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
    assert d["Q1"]["disposition"] == "answered"
    await close_runtime(rt)


def test_empty_provenance_list_is_stub():
    from coscientist.core.domain import worst_provenance

    assert worst_provenance() == "stub"
