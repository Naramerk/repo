"""M1 PR7: InputSpec.kind + uses_claim edge; AttemptStarted bindings."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.claims.graph import GraphEdge, GraphNode
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.ids import experiment_node_id
from coscientist.core.events.upcasters import upcast
from coscientist.core.execution.domain import InputSpec, TaskSpec
from coscientist.core.execution.inputs import (
    InputUnavailable,
    bind_inputs,
    input_binding_digest,
)
from tests.helpers import close_runtime, open_runtime
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
        claim_provenance={"cl_1": "real"},
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


def test_claim_unknown_provenance_defaults_to_stub():
    """Missing claim provenance attrs must not look real."""
    child = _task(
        "B",
        None,
        inputs=[InputSpec(kind="claim", ref="cl_unk")],
    )
    bindings, prov = bind_inputs(
        child,
        dep_tasks={},
        dep_outputs={},
        dep_provenance={},
        known_claim_ids={"cl_unk"},
        claim_provenance={},
    )
    assert bindings[0].source == "stub"
    assert prov == "stub"


@pytest.mark.skip(reason="schema v1: production upcast chains removed")
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
    assert v == 13
    inp = p["spec"]["inputs"][0]
    assert inp["kind"] == "artifact"
    assert inp["ref"] is None
    assert inp["quorum"] is None
    assert p["spec"]["sandbox_timeout_s"] is None
    assert p["spec"]["budget_share"] is None


@pytest.mark.skip(reason="schema v1: production upcast chains removed")
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
    # …v7 tainted (PR18) → v8 inputs_digest (#23) → v9 attempt_role (PR20).
    assert v == 9
    assert p["inputs"] == []
    assert p["limitations"] == []
    assert p["allocated_budget_rub"] is None
    assert p["budget_limit_rub"] is None
    assert p["tainted_context"] is False
    assert p.get("inputs_digest") is None
    assert p["attempt_role"] == "agent"


@pytest.mark.asyncio
async def test_accept_plan_verdict_kind_requires_ref(tmp_path, clock, runtime_cls):
    """PR8: kind=verdict is accepted; missing ref still rejected."""
    sid = "st_verdict_ref"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
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
    pid = await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                inputs=[InputSpec(kind="verdict")],
        required=True, tolerate_failed_dep=False)
        ],
        questions=[],
    )
    assert pid == ""
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "PlanRefused"]
    assert refused and "verdict requires ref" in str(refused[0].payload.get("detail") or "")
    assert any(e.type == "StudyFinalized" for e in events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_validates_input_spec_fields(tmp_path, clock, runtime_cls):
    sid = "st_inp_val"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
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
    pid = await rt.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                inputs=[InputSpec(kind="artifact")],
        required=True, tolerate_failed_dep=False)
        ],
        questions=[],
    )
    assert pid == ""
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "PlanRefused"]
    assert refused and "artifact requires from_task" in str(refused[0].payload.get("detail") or "")
    await close_runtime(rt)

    sid2 = "st_inp_val2"
    rt2 = await open_runtime(tmp_path, sid2, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt2.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid2,
        finalize_reserve_s=5,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt2.start_attempt(key)
        await rt2.cancel_task(key, reason="cancelled")
    pid2 = await rt2.accept_plan(
        [
            TaskSpec(
                task_key="E1",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                inputs=[InputSpec(kind="claim")],
        required=True, tolerate_failed_dep=False)
        ],
        questions=[],
    )
    assert pid2 == ""
    events2 = await rt2.store.read_all()
    refused2 = [e for e in events2 if e.type == "PlanRefused"]
    assert refused2 and "claim requires ref" in str(refused2[0].payload.get("detail") or "")
    await close_runtime(rt2)


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
        required=True, tolerate_failed_dep=False)
        ],
        questions=[],
    )
    aid = await rt.start_attempt("E1")
    exp_id = experiment_node_id(sid, "E1")
    assert rt.graph.get_node(exp_id) is not None
    edges = [e for e in rt.graph.edges_from(exp_id) if e.kind == "uses_claim"]
    assert len(edges) == 1 and edges[0].dst_id == claim_id
    started = next(e for e in await rt.store.read_all() if e.type == "AttemptStarted" and e.entity_key == aid)
    assert started.schema_version == 1
    assert started.payload.get("tainted_context") is False
    assert started.payload.get("inputs_digest")
    assert started.payload.get("attempt_role") == "agent"
    assert len(started.payload["inputs"]) == 1
    assert started.payload["inputs"][0]["kind"] == "claim"
    assert started.payload["inputs"][0]["digest"]
    assert started.payload["inputs"][0].get("retrieved_at")
    # No plan shares → no P2 renorm (M1 PR12 / M3).
    assert started.payload.get("share_renorm") is False
    await close_runtime(rt)

    # Replay: bindings survive reopen.
    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    assert rt2.attempts[aid]["inputs"][0]["ref"] == claim_id
    assert rt2.attempts[aid]["inputs"][0]["digest"] == started.payload["inputs"][0]["digest"]
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_stub_claim_caps_via_executor_record(tmp_path, clock, runtime_cls):
    """Production path: Executor.record writes claim provenance; no manual attrs."""
    from coscientist.core.events.ids import claim_node_id
    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle, RequirementRef
    from tests.helpers import attach_executor

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
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    from coscientist.core.execution.domain import QuestionSpec

    # Claim ids are namespaced per attempt (PR8); first attempt id is deterministic.
    aid0_expected = f"{sid}/E0#1"
    claim_id = claim_node_id(sid, f"{aid0_expected}:from_E0")
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
        required=True, tolerate_failed_dep=False),
            TaskSpec(
                task_key="E1",
                title="consumer",
                goal="g",
                route="experiment",
                depends_on=["E0"],
                inputs=[InputSpec(kind="claim", ref=claim_id)],
        required=True, tolerate_failed_dep=False),
        ],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    aid0 = await rt.start_attempt("E0", provenance="stub")
    assert aid0 == aid0_expected
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
            claims=[{"id": "from_E0", "target": "Q1"}],
        ),
    )
    node = rt.graph.get_node(claim_id)
    assert node is not None
    assert node.attrs.get("provenance") == "stub"
    assert node.attrs.get("attempt_id") == aid0
    aid1 = await rt.start_attempt("E1")
    assert rt.attempts[aid1]["provenance"] == "stub"
    assert rt.attempts[aid1]["inputs"][0]["source"] == "stub"
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

    from coscientist.core.execution.domain import Task, TaskKind, TaskState

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
        required=True, tolerate_failed_dep=False),
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


@pytest.mark.asyncio
async def test_replay_attempt_started_v2_journal(tmp_path, clock, runtime_cls):
    """Old AttemptStarted schema_version=2 (no inputs) upcasts to [] on reopen."""
    sid = "st_as_v2"
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
    # Seed a task, then inject a pre-PR7 AttemptStarted (v2, no inputs field).
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
        required=True, tolerate_failed_dep=False)
        ],
        questions=[],
    )
    tid = f"{sid}/E1"
    aid = f"{tid}#1"
    await rt._append(
        [
            EventEnvelope(
                study_id=sid,
                type="AttemptStarted",
                entity_key=aid,
                idem_key=make_idem("AttemptStarted", aid),
                payload={
                    "attempt_id": aid,
                    "n": 1,
                    "dispatch_id": f"dispatch:{aid}",
                    "route_returned": False,
                    "closed": False,
                    "close_reason": None,
                    "provenance": "real",
                    "agent_output_digest": None,
                    "deadline": (clock.now() + timedelta(seconds=30)).isoformat(),
                    "task_id": tid,
                    "study_id": sid,
                },
                ts=clock.now(),
                schema_version=2,
            )
        ]
    )
    await close_runtime(rt)

    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    assert "inputs" in rt2.attempts[aid]
    assert rt2.attempts[aid]["inputs"] == []
    await close_runtime(rt2)
