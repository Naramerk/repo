"""Strict xfail stubs that call real APIs (must be able to XPASS later).

``raises=AssertionError`` so incidental KeyError/RuntimeError do not count as xfail.
PR 4 budget-zero intake is covered by ``tests/test_intake.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coscientist.core.execution.domain import (
    HitlPolicy,
    Study,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from tests.helpers import REQUIRED_STUDY_CAPS


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="PR 6/8: based_on required for counted dispositions",
)
def test_based_on_required_for_counted_dispositions():
    from coscientist.core.finalize.finalize_nodes import graph_rows_from_finalized

    _nodes, edges = graph_rows_from_finalized(
        "st_bo",
        {
            "question_dispositions": {
                "Q1": {"disposition": "answered", "reason": None, "formulation": "x"},
            },
            "terminal_reason": "completed",
        },
        created_at="2026-01-01T00:00:00+00:00",
    )
    based = [e for e in edges.values() if e.get("kind") == "based_on"]
    assert based, "answered conclusion must carry ≥1 based_on"


def test_question_disposition_computed_from_verdict():
    """PR8: counting claims on graph → answered via verdict."""
    from coscientist.core.claims.dispositions import dispositions
    from coscientist.core.claims.graph import GraphEdge, GraphNode, GraphStore, default_execution_registry
    from coscientist.core.events.ids import question_node_id
    from coscientist.core.execution.inputs import claim_nodes_from_attempt

    sid = "st"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    g = GraphStore(default_execution_registry())
    qnid = question_node_id(sid, "Q1")
    g.upsert_node(
        GraphNode(
            node_id=qnid,
            kind="question",
            study_id=sid,
            ref_id="Q1",
            attrs={"formulation": "why"},
        )
    )
    aid = f"{sid}/E1#1"
    evs, drops = claim_nodes_from_attempt(
        study_id=sid,
        attempt_id=aid,
        provenance="real",
        claims=[{"id": "C1", "target": "Q1", "target_kind": "question", "checked_against": "value_match"}],
        now=now,
        graph=g,
        derived={
            "criterion_status": "met",
            "evidence_kind": "computational",
            "frozen": True,
            "lit_own_criterion": False,
            "accepted": True,
            "validity_ok": True,
            "artifact_id": "A1",
            "sesoi": 0.5,
            "uses_sesoi": True,
            "used_sesoi": True,
            "predicts_null": False,
            "predicted_direction": "positive",
            "effect": {
                "est": 1.5,
                "ci_low": 1.5,
                "ci_high": 1.5,
                "producer": "agent",
                "trusted": False,
                "core_evaluated": True,
                "countable": True,
                # Bound at claim authorship (not invented from aid at read time).
                "attempt_id": aid,
                "produced_by_call_id": f"{aid}:derived",
            },
            "claim_outcome": "consistent",
            "severe": True,
            "check_role": "prediction",
            "refute_capable": True,
        },
    )
    assert drops == []
    for ev in evs:
        if ev.type == "GraphNodeUpserted":
            g.upsert_node(GraphNode.model_validate(ev.payload))
        elif ev.type == "GraphEdgeAdded":
            g.add_edge(GraphEdge.model_validate(ev.payload))

    t = Task(
        task_id="st/E1",
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
        deadline=now,
        created_at=now,
        **REQUIRED_STUDY_CAPS,
    )
    plan = {
        "plan_id": "pl",
        "questions": [{"id": "Q1", "text": "why", "final_type": "verdict"}],
        "tasks": [{"task_key": "E1", "addresses": ["Q1"]}],
    }
    d = dispositions(
        tasks={"st/E1": t},
        plans={"pl": plan},
        graph=g,
        study=study,
        results={aid: {"outcome": "SUCCESS"}},
    )
    # PR8b: single severe confirm → survived (answered needs supported).
    assert d["Q1"]["disposition"] == "survived"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="PR 8: explicit QuestionDisposed infeasible",
)
def test_question_disposition_explicit_infeasible():
    from coscientist.core.claims.dispositions import dispositions

    plan = {
        "plan_id": "pl",
        "questions": [{"id": "Q1", "text": "why", "final_type": "verdict"}],
        "tasks": [],
    }
    d = dispositions(tasks={}, plans={"pl": plan})
    assert d["Q1"]["disposition"] == "infeasible"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="PR 8: all_done→infeasible; PR2 only human_timeout|completed",
)
def test_terminal_reason_trigger_all_done_infeasible():
    from coscientist.core.finalize.finalize import terminal_reason_for

    tr = terminal_reason_for(
        "all_done",
        tasks={},
        question_dispositions={"Q1": {"disposition": "infeasible", "reason": "x"}},
    )
    assert tr.value == "infeasible"
