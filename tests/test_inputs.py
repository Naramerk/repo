"""InputBinding modes use_partial|simulate|forbid."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coscientist.core.execution.domain import (
    InputSpec,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.execution.inputs import InputUnavailable, bind_inputs, worst_provenance


def _task(key: str, outcome: TaskOutcome | None, *, inputs=None):
    return Task(
        task_id=f"st/{key}",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key=key,
            title=key,
            goal="g",
            route="experiment",
            inputs=inputs or [],
        ),
        state=TaskState.DONE if outcome else TaskState.PENDING,
        outcome=outcome,
        deadline=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_forbid_failed_dep_raises():
    dep = _task("A", TaskOutcome.FAILED)
    child = _task(
        "B",
        None,
        inputs=[InputSpec(from_task="A", output="main", mode="forbid")],
    )
    child = child.model_copy(update={"state": TaskState.PENDING, "outcome": None})
    with pytest.raises(InputUnavailable):
        bind_inputs(
            child,
            dep_tasks={"A": dep},
            dep_outputs={"A": {}},
            dep_provenance={"A": "real"},
        )


def test_simulate_when_missing():
    dep = _task("A", TaskOutcome.FAILED)
    child = _task(
        "B",
        None,
        inputs=[InputSpec(from_task="A", output="main", mode="simulate")],
    )
    child = child.model_copy(update={"state": TaskState.PENDING, "outcome": None})
    bindings, prov = bind_inputs(
        child,
        dep_tasks={"A": dep},
        dep_outputs={"A": {}},
        dep_provenance={"A": "real"},
    )
    assert bindings[0].source == "stub"
    assert prov == "stub"


def test_worst_provenance_order():
    assert worst_provenance("real", "partial") == "partial"
    assert worst_provenance("partial", "stub") == "stub"
