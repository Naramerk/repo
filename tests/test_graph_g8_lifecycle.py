"""G8: lifecycle create_study / accept_plan write graph rows via G7 writer (#84)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.claims.graph import GraphEdge, GraphNode
from coscientist.core.claims.graph_writer import graph_edge_added, graph_node_upserted
from coscientist.core.claims.rubric import freeze_rubric
from coscientist.core.claims.rubric_types import Rubric, RubricItem
from coscientist.core.events.ids import criterion_node_id, question_node_id
from coscientist.core.execution.domain import (
    OutputSpec,
    QuestionSpec,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.plan.decomposes import DECOMPOSITION_COVERS_ITEM_ID
from tests.helpers import NEED_OFF, close_runtime, open_runtime


def _decomp_rubric() -> Rubric:
    return freeze_rubric(
        Rubric(
            items=[
                RubricItem(
                    id=DECOMPOSITION_COVERS_ITEM_ID,
                    text="декомпозиция покрывает исходный вопрос",
                    required=True,
                    check="judge",
                )
            ],
            aggregate="all_required",
        )
    )


def _exp(*, addresses: list[str], rubric: Rubric) -> TaskSpec:
    return TaskSpec(
        task_key="E0",
        title="E0",
        goal="g",
        route="experiment",
        depends_on=["PLAN"],
        addresses=addresses,
        expected_outputs=[
            OutputSpec(
                name="main",
                media_type="text/markdown",
                role="report",
                rubric=rubric,
            )
        ],
        criteria=[RequirementRef(template_id="rubric_check")],
    )


async def _boot(tmp_path, clock, sid: str):
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="t",
        request="g8 lifecycle via G7",
        catalog_version=4,
        deadline=clock.now() + timedelta(hours=1),
        finalize_reserve_s=30,
        sid=sid,
        max_depth=8,
        max_tasks=64,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    return rt


@pytest.mark.asyncio
async def test_create_study_study_node_via_g7_writer(tmp_path, clock):
    """StudyCreated batch includes GraphNodeUpserted built like graph_node_upserted."""
    sid = "st_g8_create"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="t",
        request="g8 create",
        catalog_version=4,
        deadline=clock.now() + timedelta(hours=1),
        finalize_reserve_s=30,
        sid=sid,
        require_hypotheses=False,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    events = await rt.store.read_all()
    study_nodes = [
        e
        for e in events
        if e.type == "GraphNodeUpserted" and (e.payload or {}).get("kind") == "study"
    ]
    assert len(study_nodes) == 1
    ev = study_nodes[0]
    assert ev.entity_key == f"{sid}:study"
    assert (ev.payload or {}).get("node_id") == f"{sid}:study"
    # G7 idem = type:node_id:digest (content digest; created_at excluded).
    rebuilt = graph_node_upserted(
        node=GraphNode.model_validate(ev.payload),
        ts=ev.ts,
        registry=rt.graph.registry,
        idem_extra=(),
    )
    assert ev.idem_key == rebuilt.idem_key
    assert ev.schema_version == rebuilt.schema_version
    assert ev.idem_key.startswith("GraphNodeUpserted:")
    # Digest segment present (pre-G8 was GraphNodeUpserted:{sid}:study only).
    assert len(ev.idem_key.split(":")) >= 3
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_accept_plan_questions_edges_criteria_via_g7(tmp_path, clock):
    """accept_plan question/decomposes/criterion rows match G7 idem + schema_version."""
    sid = "st_g8_accept"
    rt = await _boot(tmp_path, clock, sid)
    rub = _decomp_rubric()
    q_root = QuestionSpec(id="Q", text="root", final_type="artifact")
    q_a = QuestionSpec(
        id="Q.a",
        text="child A",
        parent_id="Q",
        origin="explicit",
        final_type="artifact",
    )
    pid = await rt.accept_plan(
        [_exp(addresses=["Q.a"], rubric=rub)],
        questions=[q_root],
        subquestions=[q_a],
        rollup="any",
        hypotheses=NEED_OFF,
        literature=NEED_OFF,
        expand_after=[],
        loops=[],
        spec_output=None,
    )
    assert pid
    events = await rt.store.read_all()
    qnid_root = question_node_id(sid, "Q")
    qnid_a = question_node_id(sid, "Q.a")
    q_events = {
        e.payload["node_id"]: e
        for e in events
        if e.type == "GraphNodeUpserted" and (e.payload or {}).get("kind") == "question"
    }
    assert qnid_root in q_events and qnid_a in q_events
    for _nid, ev in q_events.items():
        rebuilt = graph_node_upserted(
            node=GraphNode.model_validate(ev.payload),
            ts=ev.ts,
            registry=rt.graph.registry,
            idem_extra=(pid,),
        )
        assert ev.idem_key == rebuilt.idem_key
        assert ev.schema_version == rebuilt.schema_version

    edge_evs = [
        e
        for e in events
        if e.type == "GraphEdgeAdded" and (e.payload or {}).get("kind") == "decomposes"
    ]
    assert len(edge_evs) == 1
    edge_ev = edge_evs[0]
    edge = GraphEdge.model_validate(edge_ev.payload)
    src = GraphNode.model_validate(q_events[qnid_a].payload)
    dst = GraphNode.model_validate(q_events[qnid_root].payload)
    rebuilt_edge = graph_edge_added(
        edge=edge,
        ts=edge_ev.ts,
        src=src,
        dst=dst,
        registry=rt.graph.registry,
        idem_extra=(),
    )
    assert edge_ev.idem_key == rebuilt_edge.idem_key
    assert edge_ev.payload["attrs"]["origin"] == "explicit"

    dig = rub.digest
    assert isinstance(dig, str) and dig
    rnid = criterion_node_id(sid, f"rubric:{dig}")
    crit_evs = [
        e
        for e in events
        if e.type == "GraphNodeUpserted"
        and (e.payload or {}).get("node_id") == rnid
    ]
    assert len(crit_evs) == 1
    crit_ev = crit_evs[0]
    rebuilt_crit = graph_node_upserted(
        node=GraphNode.model_validate(crit_ev.payload),
        ts=crit_ev.ts,
        registry=rt.graph.registry,
        idem_extra=(pid,),
    )
    assert crit_ev.idem_key == rebuilt_crit.idem_key
    assert (crit_ev.payload.get("attrs") or {}).get("rubric_digest") == dig
    await close_runtime(rt)
