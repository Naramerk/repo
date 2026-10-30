"""Fail-closed totality of dispositions() on validation-bypassed Task objects.

Task.spec / Task.task_id are required pydantic fields; dispositions() reads them
directly. An object that bypassed validation (model_construct) and misses one of
them must not crash the caller: the public dispositions() wrapper stays total and
degrades to the honest not_attempted fallback for every known question.
"""

from __future__ import annotations

from coscientist.core.claims.dispositions import dispositions
from coscientist.core.claims.reasons import Disposition, NotAttemptedReason
from coscientist.core.execution.domain import Task, TaskSpec, TaskState


def test_task_without_spec_falls_closed() -> None:
    """A Task bypassing validation without spec degrades to the total fallback."""
    t = Task.model_construct(task_id="t1", reason=None)
    plans: dict[str, dict[str, object]] = {
        "p1": {"questions": [{"id": "q1", "text": "Q?"}]},
    }

    result = dispositions(tasks={"t1": t}, plans=plans)

    assert result["q1"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert result["q1"]["reason"] == NotAttemptedReason.NO_COUNTING_CLAIMS.value
    assert result["q1"]["formulation"] == "Q?"


def test_task_without_task_id_and_empty_task_key_falls_closed() -> None:
    """Cover union no longer invents an empty key: missing task_id fails closed.

    Previously such a task silently contributed an empty cover key; now the
    direct attribute read raises inside the total wrapper and every question
    gets the honest not_attempted fallback.
    """
    t = Task.model_construct(
        spec=TaskSpec(
            task_key="",
            title="t",
            goal="g",
            route="r",
            addresses=["q1"],
        ),
        state=TaskState.PENDING,
        reason=None,
    )
    plans: dict[str, dict[str, object]] = {
        "p1": {
            "questions": [{"id": "q1", "text": "Q?"}],
            "tasks": [{"task_key": "", "addresses": ["q1"]}],
        },
    }

    result = dispositions(tasks={"t1": t}, plans=plans)

    assert result["q1"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    assert result["q1"]["reason"] == NotAttemptedReason.NO_COUNTING_CLAIMS.value
