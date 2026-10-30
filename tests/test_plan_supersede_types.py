"""Plan-supersede helpers: honestly typed sources (PR core-supersede-bh).

Covers the declared behavior changes of the typed signatures: a runtime
missing a declared read-surface slot now fails with AttributeError instead of
falling through a duck-typed probe with a silent default. The runtime stubs
declare the missing SupersedeRuntime slots as class annotations without
assignment, so attribute access raises AttributeError while the stub still
type-checks against the protocol. Positive paths use the real domain types
only: production callers (plan/expand.py, plan/expand_critic.py,
claims/claims.py) pass exactly StudyRuntime, Task, TaskSpec and GraphStore,
and dict/TaskSpec-shaped substitutes cannot occur past TaskSpec.model_validate
(plan/expand.py).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from coscientist.core.claims.graph import GraphStore, registry_from_schema
from coscientist.core.execution.domain import (
    Catalog,
    HitlPolicy,
    Study,
    Task,
    TaskKind,
    TaskSpec,
    TaskState,
)
from coscientist.core.plan.supersede import (
    _has_refute_tests_edge,
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


def _study() -> Study:
    now = datetime.now(UTC)
    return Study(
        study_id="s1",
        profile="research",
        request="q",
        catalog_version=4,
        hitl_policy=HitlPolicy(),
        deadline=now,
        created_at=now,
        require_hypotheses=False,
        confirmations_needed=1,
        max_expansions=2,
        max_attempts=3,
        refutation_priority=False,
        refutations_needed=1,
        supported_needed=2,
    )


def _task(key: str, *, addresses: list[str] | None = None) -> Task:
    """Real open Task over a validated spec (production-shaped input)."""
    return Task(
        task_id=key,
        kind=TaskKind.EXPERIMENT,
        spec=_spec(key, addresses=addresses),
        state=TaskState.PENDING,
        deadline=datetime.now(UTC),
    )


class _RuntimeNoReads:
    """Runtime stub with every protocol slot declared and none assigned."""

    study: Study | None
    graph: GraphStore
    results: Mapping[str, Mapping[str, object]]
    catalog: Catalog
    tasks: Mapping[str, Task]


class _RuntimeStudyGraphOnly:
    """Runtime stub with real study/graph slots; the rest are declared only."""

    results: Mapping[str, Mapping[str, object]]
    catalog: Catalog
    tasks: Mapping[str, Task]

    def __init__(self) -> None:
        self.study = _study()
        self.graph = GraphStore(registry_from_schema())


class _RuntimeNoCatalog:
    """Runtime stub whose ``study`` slot is None and catalog slot is absent.

    ``graph`` is a real store: the protocol types it non-optional.
    """

    study: Study | None = None
    results: Mapping[str, Mapping[str, object]]
    catalog: Catalog
    tasks: Mapping[str, Task]

    def __init__(self) -> None:
        self.graph = GraphStore(registry_from_schema())


class _RuntimeNoStudy:
    """Runtime stub with a real graph slot and no ``study`` slot."""

    study: Study | None
    results: Mapping[str, Mapping[str, object]]
    catalog: Catalog
    tasks: Mapping[str, Task]

    def __init__(self) -> None:
        self.graph = GraphStore(registry_from_schema())


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


def test_countable_refutation_requires_runtime_attributes() -> None:
    # Was: missing study/graph/results silently returned 0. Now: AttributeError.
    with pytest.raises(
        AttributeError,
        match=re.escape("'_RuntimeNoReads' object has no attribute 'study'"),
    ):
        countable_refutation_n(_RuntimeNoReads(), "Q1")


def test_countable_refutation_requires_results_attribute() -> None:
    # Was: the results probe defaulted silently to None; with study and graph
    # present the old code kept counting (returned 0 on an empty store) instead
    # of failing. Now: the results read surface is required.
    with pytest.raises(
        AttributeError,
        match=re.escape("'_RuntimeStudyGraphOnly' object has no attribute 'results'"),
    ):
        countable_refutation_n(_RuntimeStudyGraphOnly(), "Q1")


def test_task_protection_requires_catalog_attribute() -> None:
    # Was: a missing catalog/templates/criteria probe fell through to "not
    # capable" and the task silently lost protection. Now: the catalog read
    # surface is required before criteria are inspected (the linked task keeps
    # _task_links_to_target on the addresses path, so the catalog is reached).
    with pytest.raises(
        AttributeError,
        match=re.escape("'_RuntimeNoCatalog' object has no attribute 'catalog'"),
    ):
        task_protected_from_agent_skip(_RuntimeNoCatalog(), _task("A", addresses=["Q1"]))


def test_refute_tests_edge_requires_study_attribute() -> None:
    # Was: a runtime without ``study`` defaulted to None and the refute tests
    # edge was silently treated as absent. Now: the study slot is required and
    # is read before the task spec.
    with pytest.raises(
        AttributeError,
        match=re.escape("'_RuntimeNoStudy' object has no attribute 'study'"),
    ):
        _has_refute_tests_edge(_RuntimeNoStudy(), _task("A", addresses=["Q1"]))
