"""M1 PR7: InputSpec.kind + uses_claim edge; verdict snapshot digest."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.domain import InputSpec, TaskSpec
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.graph import GraphNode
from coscientist.core.inputs import (
    InputUnavailable,
    bind_inputs,
    experiment_node_id,
    verdict_snapshot_digest,
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
    child = child.model_copy(update={"state": child.state})
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
    assert bindings[0].artifact_id == ""


def test_bind_verdict_input_snapshots_digest():
    child = _task(
        "B",
        None,
        inputs=[InputSpec(kind="verdict", ref="H1")],
    )
    bindings, _ = bind_inputs(
        child,
        dep_tasks={},
        dep_outputs={},
        dep_provenance={},
        verdicts={"H1": "supported"},
    )
    assert bindings[0].kind == "verdict"
    assert bindings[0].digest == verdict_snapshot_digest("H1", "supported")
    with pytest.raises(InputUnavailable, match="verdict_unavailable"):
        bind_inputs(
            child,
            dep_tasks={},
            dep_outputs={},
            dep_provenance={},
            verdicts={},
        )


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


@pytest.mark.asyncio
async def test_uses_claim_edge(tmp_path, clock, runtime_cls):
    """P2 with InputSpec.kind=claim adds uses_claim: experiment → claim."""
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
                    attrs={},
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
    await rt.start_attempt("E1")
    exp_id = experiment_node_id(sid, "E1")
    assert rt.graph.get_node(exp_id) is not None
    edges = [e for e in rt.graph.edges_from(exp_id) if e.kind == "uses_claim"]
    assert len(edges) == 1
    assert edges[0].dst_id == claim_id
    journal = await rt.store.read_all()
    assert any(e.type == "GraphEdgeAdded" and e.payload.get("kind") == "uses_claim" for e in journal)
    await close_runtime(rt)
