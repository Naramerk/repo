"""M1 PR8: verdict() runtime wiring, kind=verdict, claim carry-overs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coscientist.core.claims import CLAIM_ATTR_WHITELIST, claims_missing_about
from coscientist.core.dispositions import dispositions
from coscientist.core.domain import (
    InputSpec,
    QuestionSpec,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.graph import GraphNode
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


def test_reupsert_keeps_worst_provenance():
    """Carry-over #1: stub claim cannot be laundered to real via same id."""
    from coscientist.core.graph import GraphStore, default_execution_registry

    g = GraphStore(default_execution_registry())
    sid = "st_launder"
    cid = claim_node_id(sid, "C1")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    # First upsert: stub
    evs1 = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=f"{sid}/E0#1",
        provenance="stub",
        claims=[{"id": "C1", "target": "Q1", "criterion_status": "met"}],
        now=now,
        graph=g,
    )
    for ev in evs1:
        if ev.type == "GraphNodeUpserted":
            g.upsert_node(GraphNode.model_validate(ev.payload))
        elif ev.type == "GraphEdgeAdded":
            from coscientist.core.graph import GraphEdge

            g.add_edge(GraphEdge.model_validate(ev.payload))
    assert g.get_node(cid).attrs["provenance"] == "stub"
    # Re-upsert same id with real — must stay stub
    evs2 = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=f"{sid}/E1#1",
        provenance="real",
        claims=[{"id": "C1", "target": "Q1", "criterion_status": "met", "secret": "x"}],
        now=now,
        graph=g,
    )
    node_ev = next(e for e in evs2 if e.type == "GraphNodeUpserted" and e.entity_key == cid)
    assert node_ev.payload["attrs"]["provenance"] == "stub"
    assert "secret" not in node_ev.payload["attrs"]


def test_claim_attrs_whitelisted():
    """Carry-over #3: only whitelisted agent fields land in attrs."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    evs = claim_nodes_from_attempt(
        study_id="st_w",
        attempt_id="st_w/E#1",
        provenance="real",
        claims=[
            {
                "id": "C1",
                "target": "Q1",
                "criterion_status": "met",
                "stance": "supports",  # must not be copied
                "evil": 1,
                "reason": "ok",
            }
        ],
        now=now,
        graph=None,
    )
    attrs = next(e for e in evs if e.type == "GraphNodeUpserted").payload["attrs"]
    assert "evil" not in attrs
    assert "stance" not in attrs
    assert attrs["reason"] == "ok"
    assert attrs["target"] == "Q1"
    assert set(attrs) <= (CLAIM_ATTR_WHITELIST | {"provenance", "attempt_id", "attempt_outcome"})


def test_about_edge_emitted_and_cardinality():
    """Carry-over #3: about edge created; missing about detected."""
    from coscientist.core.graph import GraphEdge, GraphStore, default_execution_registry

    g = GraphStore(default_execution_registry())
    sid = "st_about"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    evs = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=f"{sid}/E#1",
        provenance="real",
        claims=[{"id": "C1", "target": "Q1", "criterion_status": "met"}],
        now=now,
        graph=g,
    )
    for ev in evs:
        if ev.type == "GraphNodeUpserted":
            g.upsert_node(GraphNode.model_validate(ev.payload))
        elif ev.type == "GraphEdgeAdded":
            g.add_edge(GraphEdge.model_validate(ev.payload))
    cid = claim_node_id(sid, "C1")
    about = [e for e in g.edges_from(cid) if e.kind == "about"]
    assert len(about) == 1
    assert about[0].dst_id == question_node_id(sid, "Q1")
    assert claims_missing_about(g) == []
    # Orphan claim without about
    orphan = claim_node_id(sid, "orphan")
    g.upsert_node(
        GraphNode(
            node_id=orphan,
            kind="claim",
            study_id=sid,
            ref_id=orphan,
            attrs={"provenance": "real"},
        )
    )
    assert orphan in claims_missing_about(g)


@pytest.mark.asyncio
async def test_commit_failure_path_drops_claims(tmp_path, clock, runtime_cls):
    """Carry-over #2: CatalogError → _commit_failure passes claims=[]."""
    from unittest.mock import patch

    from coscientist.core.artifacts import make_tool_ref
    from coscientist.core.domain import AgentTaskOutput, ArtifactHandle, RequirementRef
    from coscientist.core.errors import CatalogError

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
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    claim_id = claim_node_id(sid, "cf")
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
        questions=[],
    )
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
                claims=[{"id": claim_id, "target": "Q1", "criterion_status": "met"}],
            ),
        )
    assert result.outcome.value == "FAILED"
    assert rt.graph.get_node(claim_id) is None
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_failed_attempt_claims_not_emitted(tmp_path, clock, runtime_cls):
    """Carry-over #2: FAILED TaskOutcome does not write claim nodes."""
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
    ex, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    claim_id = claim_node_id(sid, "bad")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="producer",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                criteria=[],  # defaults → artifact_present fails without outputs
            )
        ],
        questions=[],
    )
    aid0 = await rt.start_attempt("E0")
    # FAILED attempt (missing artifact) must not emit claims
    result = await ex.record(
        aid0,
        AgentTaskOutput(
            summary="fail",
            outputs={},
            values={},
            claims=[{"id": claim_id, "target": "Q1", "criterion_status": "met"}],
        ),
    )
    assert result.outcome.value == "FAILED"
    assert rt.graph.get_node(claim_id) is None
    known, _ = collect_bindable_claims(rt.graph, results=rt.results)
    assert claim_id not in known
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_failed_producer_claim_not_bindable(tmp_path, clock, runtime_cls):
    """Carry-over #2 (bind-time): claim with FAILED producer is unavailable."""
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
    # Plant a claim that looks like it came from a FAILED attempt.
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
    """kind=verdict binds persisted snapshot digest at P2."""
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
    assert bindings[0].ref == "Q1"
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
async def test_verdict_input_journaled_on_attempt(tmp_path, clock, runtime_cls):
    """P2 kind=verdict: digest from graph claims + study k_ref."""
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
        refutations_needed=1,
        refutation_priority=False,
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    claim_id = claim_node_id(sid, "c1")
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
            claims=[
                {
                    "id": claim_id,
                    "target": "Q1",
                    "criterion_status": "met",
                    "evidence_kind": "computational",
                }
            ],
        ),
    )
    node = rt.graph.get_node(claim_id)
    assert node is not None
    about = [e for e in rt.graph.edges_from(claim_id) if e.kind == "about"]
    assert len(about) == 1
    aid1 = await rt.start_attempt("E1")
    started = next(
        e
        for e in await rt.store.read_all()
        if e.type == "AttemptStarted" and e.entity_key == aid1
    )
    assert started.payload["inputs"][0]["kind"] == "verdict"
    assert started.payload["inputs"][0]["ref"] == "Q1"
    assert started.payload["inputs"][0]["digest"]
    await close_runtime(rt)


def test_dispositions_from_verdict_counting_claims():
    """PR8: counting claims → answered via verdict()."""
    from coscientist.core.domain import HitlPolicy, Study
    from coscientist.core.graph import GraphEdge, GraphStore, default_execution_registry

    sid = "st_disp"
    g = GraphStore(default_execution_registry())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for ev in claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=f"{sid}/E1#1",
        provenance="real",
        claims=[
            {
                "id": "C1",
                "target": "Q1",
                "criterion_status": "met",
                "evidence_kind": "computational",
                "artifact_id": "A1",
            }
        ],
        now=now,
        graph=g,
    ):
        if ev.type == "GraphNodeUpserted":
            g.upsert_node(GraphNode.model_validate(ev.payload))
        elif ev.type == "GraphEdgeAdded":
            g.add_edge(GraphEdge.model_validate(ev.payload))

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
        results={f"{sid}/E1#1": {"outcome": "SUCCESS"}},
    )
    assert d["Q1"]["disposition"] == "answered"
    assert d["Q1"]["verdict"] == "supported"


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
    claim_id = claim_node_id(sid, "ans")
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
            claims=[
                {
                    "id": claim_id,
                    "target": "Q1",
                    "criterion_status": "met",
                    "evidence_kind": "computational",
                    "artifact_id": "A1",
                }
            ],
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
