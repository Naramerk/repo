"""M1 PR7: InputSpec.kind + uses_claim edge; AttemptStarted bindings."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.domain import InputSpec, TaskSpec
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.graph import GraphEdge, GraphNode
from coscientist.core.ids import experiment_node_id
from coscientist.core.inputs import (
    InputUnavailable,
    bind_inputs,
    input_binding_digest,
)
from coscientist.core.runtime_factory import close_runtime, open_runtime
from coscientist.core.upcasters import upcast
from tests.test_inputs import _task


def test_bind_claim_input_requires_known_ref():
    child = _task(
        "B",
        None,
        inputs=[InputSpec(kind="claim", ref="cl_1")],
    )
    with pytest.raises(InputUnavailable, match="claim_unavailable"):
        bind_inputs(
            child,
            dep_tasks={},
            dep_outputs={},
            dep_provenance={},
            known_claim_ids=set(),
        )
    bindings, prov = bind_inputs(
        child,
        dep_tasks={},
        dep_outputs={},
        dep_provenance={},
        known_claim_ids={"cl_1"},
    )
    assert prov == "real"
    assert bindings[0].kind == "claim"
    assert bindings[0].ref == "cl_1"
    assert bindings[0].digest == input_binding_digest(
        kind="claim",
        input_name="cl_1",
        artifact_id="",
        source="real",
        ref="cl_1",
    )


def test_claim_provenance_from_producing_attempt():
    """Claim from a stub attempt stays stub → H-b caps child provenance."""
    child = _task(
        "B",
        None,
        inputs=[InputSpec(kind="claim", ref="cl_stub")],
    )
    bindings, prov = bind_inputs(
        child,
        dep_tasks={},
        dep_outputs={},
        dep_provenance={},
        known_claim_ids={"cl_stub"},
        claim_provenance={"cl_stub": "stub"},
    )
    assert bindings[0].source == "stub"
    assert prov == "stub"


def test_upcast_task_created_input_kind_defaults():
    v, p = upcast(
        "TaskCreated",
        4,
        {
            "task_id": "st/E1",
            "kind": "experiment",
            "spec": {
                "task_key": "E1",
                "title": "e",
                "goal": "g",
                "route": "experiment",
                "inputs": [{"from_task": "A", "output": "main", "mode": "forbid"}],
            },
            "state": "PENDING",
            "deadline": "2026-01-01T00:00:00+00:00",
        },
    )
    assert v == 5
    inp = p["spec"]["inputs"][0]
    assert inp["kind"] == "artifact"
    assert inp["ref"] is None


def test_upcast_attempt_started_inputs_default():
    v, p = upcast(
        "AttemptStarted",
        2,
        {
            "attempt_id": "st/E1#1",
            "n": 1,
            "task_id": "st/E1",
            "study_id": "st",
            "provenance": "real",
            "deadline": "2026-01-01T00:00:00+00:00",
        },
    )
    assert v == 3
    assert p["inputs"] == []


@pytest.mark.asyncio
async def test_accept_plan_rejects_verdict_kind(tmp_path, clock, runtime_cls):
    sid = "st_no_verdict"
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
    with pytest.raises(ValueError, match="verdict.*PR 8"):
        await rt.accept_plan(
            [
                TaskSpec(
                    task_key="E1",
                    title="e",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    inputs=[InputSpec(kind="verdict", ref="H1")],
                )
            ],
            questions=[],
        )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_validates_input_spec_fields(tmp_path, clock, runtime_cls):
    sid = "st_inp_val"
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
    with pytest.raises(ValueError, match="artifact requires from_task"):
        await rt.accept_plan(
            [
                TaskSpec(
                    task_key="E1",
                    title="e",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    inputs=[InputSpec(kind="artifact")],
                )
            ],
            questions=[],
        )
    with pytest.raises(ValueError, match="claim requires ref"):
        await rt.accept_plan(
            [
                TaskSpec(
                    task_key="E1",
                    title="e",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    inputs=[InputSpec(kind="claim")],
                )
            ],
            questions=[],
        )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_uses_claim_edge_and_bindings_journaled(tmp_path, clock, runtime_cls):
    """P2 claim input → uses_claim edge; bindings+digest on AttemptStarted."""
    sid = "st_uses_cl"
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
    claim_id = f"{sid}:claim:C1"
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
                    ref_id="C1",
                    attrs={"provenance": "real"},
                ).model_dump(mode="json"),
                ts=clock.now(),
            )
        ]
    )
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                inputs=[InputSpec(kind="claim", ref=claim_id)],
            )
        ],
        questions=[],
    )
    aid = await rt.start_attempt("E1")
    exp_id = experiment_node_id(sid, "E1")
    assert rt.graph.get_node(exp_id) is not None
    edges = [e for e in rt.graph.edges_from(exp_id) if e.kind == "uses_claim"]
    assert len(edges) == 1 and edges[0].dst_id == claim_id
    started = next(e for e in await rt.store.read_all() if e.type == "AttemptStarted" and e.entity_key == aid)
    assert started.schema_version == 3
    assert len(started.payload["inputs"]) == 1
    assert started.payload["inputs"][0]["kind"] == "claim"
    assert started.payload["inputs"][0]["digest"]
    await close_runtime(rt)

    # Replay: bindings survive reopen.
    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    assert rt2.attempts[aid]["inputs"][0]["ref"] == claim_id
    assert rt2.attempts[aid]["inputs"][0]["digest"] == started.payload["inputs"][0]["digest"]
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_stub_claim_caps_attempt_provenance(tmp_path, clock, runtime_cls):
    sid = "st_stub_cl"
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
    claim_id = f"{sid}:claim:Cs"
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
                    ref_id="Cs",
                    attrs={"provenance": "stub"},
                ).model_dump(mode="json"),
                ts=clock.now(),
            )
        ]
    )
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                inputs=[InputSpec(kind="claim", ref=claim_id)],
            )
        ],
        questions=[],
    )
    aid = await rt.start_attempt("E1")
    assert rt.attempts[aid]["provenance"] == "stub"
    assert rt.attempts[aid]["inputs"][0]["source"] == "stub"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_graph_edge_added_fails_loudly(tmp_path, clock, runtime_cls):
    """GraphEdgeAdded with missing endpoints raises (no silent swallow)."""
    sid = "st_edge_loud"
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
    with pytest.raises(KeyError, match="edge src missing|edge dst missing"):
        await rt._append(
            [
                EventEnvelope(
                    study_id=sid,
                    type="GraphEdgeAdded",
                    entity_key="uses_claim:missing",
                    idem_key=make_idem("GraphEdgeAdded", "bad"),
                    payload=GraphEdge(
                        edge_id="uses_claim:missing",
                        kind="uses_claim",
                        study_id=sid,
                        src_id="no_exp",
                        dst_id="no_claim",
                    ).model_dump(mode="json"),
                    ts=clock.now(),
                )
            ]
        )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_replay_task_created_v4_journal(tmp_path, clock, runtime_cls):
    """Old TaskCreated schema_version=4 (no kind) upcasts on reopen."""
    from datetime import UTC, datetime

    from coscientist.core.domain import Task, TaskKind, TaskState

    sid = "st_tc_v4"
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
    # Inject a v4 TaskCreated as written before PR7 (no InputSpec.kind).
    tid = f"{sid}/LEGACY"
    legacy = Task(
        task_id=tid,
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key="LEGACY",
            title="l",
            goal="g",
            route="experiment",
            inputs=[InputSpec(from_task="A", output="main", mode="forbid")],
        ),
        state=TaskState.PENDING,
        deadline=datetime(2026, 1, 1, tzinfo=UTC),
    )
    payload = legacy.model_dump(mode="json")
    # Strip kind/ref as an old writer would have omitted them after dump defaults —
    # simulate pre-PR7 payload shape.
    for inp in payload["spec"]["inputs"]:
        inp.pop("kind", None)
        inp.pop("ref", None)
    await rt._append(
        [
            EventEnvelope(
                study_id=sid,
                type="TaskCreated",
                entity_key=tid,
                idem_key=make_idem("TaskCreated", tid),
                payload=payload,
                ts=clock.now(),
                schema_version=4,
            )
        ]
    )
    await close_runtime(rt)

    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    task = rt2.tasks[tid]
    assert task.spec.inputs[0].kind == "artifact"
    assert task.spec.inputs[0].from_task == "A"
    await close_runtime(rt2)
