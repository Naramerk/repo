"""Plan-supersede helpers: honestly typed sources (PR core-supersede-bh).

Covers the declared behavior changes of the typed signatures: inputs that
previously fell through duck-typed probes with silent defaults now fail with
AttributeError. Production callers (plan/expand.py, plan/expand_critic.py,
claims/claims.py) already pass exactly StudyRuntime, Task, TaskSpec and
GraphStore, so the positive paths below use the real domain types only.
"""

from __future__ import annotations

import re

import pytest

from coscientist.core.execution.domain import TaskSpec
from coscientist.core.plan.supersede import (
    countable_refutation_n,
    criterion_is_superseded,
    question_is_superseded,
    should_skip_for_question_supersede,
    task_protected_from_agent_skip,
    tasks_needed_by_active_questions,
)


def _spec(
    key: str,
    *,
    addresses: list[str] | None = None,
    deps: list[str] | None = None,
) -> TaskSpec:
    return TaskSpec(
        task_key=key,
        title=key,
        goal=key,
        route="experiment",
        addresses=list(addresses or []),
        depends_on=list(deps or []),
    )


def test_question_is_superseded_none_graph_returns_false() -> None:
    assert question_is_superseded(None, "s1", "Q1") is False


def test_criterion_is_superseded_none_graph_returns_false() -> None:
    assert criterion_is_superseded(None, "s1", "C1") is False


def test_tasks_needed_keeps_active_and_closes_dependencies() -> None:
    tasks = [
        _spec("T1", addresses=["Q1"], deps=["T2"]),
        _spec("T2", deps=["T3"]),
        _spec("T3", addresses=["Q2"]),
    ]
    needed = tasks_needed_by_active_questions(
        tasks=tasks,
        superseded_old={"Q2"},
        active_q_after={"Q1"},
    )
    assert needed == {"T1", "T2", "T3"}


def test_tasks_needed_rejects_dict_tasks() -> None:
    with pytest.raises(
        AttributeError,
        match=re.escape("'dict' object has no attribute 'task_key'"),
    ):
        tasks_needed_by_active_questions(
            tasks=[{"task_key": "T1", "addresses": ["Q1"]}],
            superseded_old=set(),
            active_q_after={"Q1"},
        )


def test_should_skip_true_for_solitary_superseded() -> None:
    verdict = should_skip_for_question_supersede(
        task=_spec("A", addresses=["Q1"]),
        superseded_old={"Q1"},
        active_q_after=set(),
        protected_keys=set(),
    )
    assert verdict is True


def test_should_skip_false_when_question_still_active() -> None:
    verdict = should_skip_for_question_supersede(
        task=_spec("A", addresses=["Q1"]),
        superseded_old={"Q1"},
        active_q_after={"Q1"},
        protected_keys=set(),
    )
    assert verdict is False


def test_should_skip_false_for_protected_key() -> None:
    verdict = should_skip_for_question_supersede(
        task=_spec("A", addresses=["Q1"]),
        superseded_old={"Q1"},
        active_q_after=set(),
        protected_keys={"A"},
    )
    assert verdict is False


def test_should_skip_false_for_refute_key() -> None:
    verdict = should_skip_for_question_supersede(
        task=_spec("A", addresses=["Q1"]),
        superseded_old={"Q1"},
        active_q_after=set(),
        protected_keys=set(),
        refute_keys={"A"},
    )
    assert verdict is False


def test_should_skip_false_when_blocked_question_overlaps() -> None:
    verdict = should_skip_for_question_supersede(
        task=_spec("A", addresses=["Q1"]),
        superseded_old={"Q1"},
        active_q_after=set(),
        protected_keys=set(),
        blocked_questions={"Q1"},
    )
    assert verdict is False


def test_should_skip_rejects_dict_task() -> None:
    with pytest.raises(
        AttributeError,
        match=re.escape("'dict' object has no attribute 'task_key'"),
    ):
        should_skip_for_question_supersede(
            task={"task_key": "A", "addresses": ["Q1"]},
            superseded_old={"Q1"},
            active_q_after=set(),
            protected_keys=set(),
        )


def test_countable_refutation_requires_runtime_attributes() -> None:
    # Was: missing study/graph/results silently returned 0. Now: AttributeError.
    with pytest.raises(
        AttributeError,
        match=re.escape("'object' object has no attribute 'study'"),
    ):
        countable_refutation_n(object(), "Q1")


def test_task_protected_requires_task_object() -> None:
    # Was: a bare TaskSpec was accepted via the spec-fallback probe. Now: the
    # Task state is read directly, so the wrong type fails before the runtime
    # (never reached here) is touched.
    with pytest.raises(
        AttributeError,
        match=re.escape("'TaskSpec' object has no attribute 'state'"),
    ):
        task_protected_from_agent_skip(None, _spec("A", addresses=["Q1"]))
