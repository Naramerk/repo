"""Strict xfail stubs that call real APIs (must be able to XPASS later).

``raises=AssertionError`` so incidental KeyError/RuntimeError do not count as xfail.
PR 4 budget-zero intake is covered by ``tests/test_intake.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coscientist.core.domain import (
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="PR 6/8: based_on required for counted dispositions",
)
def test_based_on_required_for_counted_dispositions():
    from coscientist.core.finalize_nodes import graph_rows_from_finalized

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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="PR 6/8: question disposition from verdict()",
)
def test_question_disposition_computed_from_verdict():
    from coscientist.core.dispositions import dispositions

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
        deadline=datetime(2026, 1, 1, tzinfo=UTC),
    )
    plan = {
        "plan_id": "pl",
        "questions": [{"id": "Q1", "text": "why"}],
        "tasks": [{"task_key": "E1", "addresses": ["Q1"]}],
    }
    d = dispositions(tasks={"st/E1": t}, plans={"pl": plan})
    assert d["Q1"]["disposition"] == "answered"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="PR 8: explicit QuestionDisposed infeasible",
)
def test_question_disposition_explicit_infeasible():
    from coscientist.core.dispositions import dispositions

    plan = {
        "plan_id": "pl",
        "questions": [{"id": "Q1", "text": "why"}],
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
    from coscientist.core.finalize import terminal_reason_for

    tr = terminal_reason_for(
        "all_done",
        tasks={},
        question_dispositions={"Q1": {"disposition": "infeasible", "reason": "x"}},
    )
    assert tr.value == "infeasible"
