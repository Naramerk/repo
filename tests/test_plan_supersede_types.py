"""Plan-supersede helpers: honestly typed sources (PR core-supersede-bh).

Covers the declared behavior changes of the typed signatures: inputs that
previously fell through duck-typed probes with silent defaults now fail with
AttributeError. Production callers (plan/expand.py, plan/expand_critic.py,
claims/claims.py) already pass exactly StudyRuntime, Task, TaskSpec and
GraphStore, so the positive paths below use the real domain types only.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from coscientist.core.claims.graph import GraphStore, registry_from_schema
from coscientist.core.execution.domain import Catalog, HitlPolicy, Study, TaskSpec, TaskState
from coscientist.core.plan.supersede import (
    _criteria_prediction_capable,
    _has_refute_tests_edge,
    _task_links_to_target,
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


class _RuntimeStudyGraphOnly:
    """Runtime stub with real study/graph slots and no other read surface."""

    def __init__(self) -> None:
        self.study = _study()
        self.graph = GraphStore(registry_from_schema())


class _RuntimeCoreNone:
    """Runtime stub whose study/graph slots exist and are None (no catalog)."""

    study: Study | None = None
    graph: GraphStore | None = None


class _SpecNoCriteria:
    """TaskSpec-shaped stub that dropped ``criteria`` (old code defaulted [])."""

    task_key = "A"
    addresses = ["Q1"]
    tests_intent = None


class _TaskWithSpecNoCriteria:
    """Task-shaped stub whose spec dropped ``criteria``."""

    state = TaskState.PENDING
    spec = _SpecNoCriteria()


class _TaskNoSpec:
    """Task-shaped stub without ``spec`` (old code probed the task itself)."""


class _SpecNoAddresses:
    """TaskSpec-shaped stub without ``addresses`` (old code defaulted [])."""

    task_key = "A"


class _RuntimeCatalogOnly:
    """Runtime stub exposing only the catalog read surface."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog


class _CatalogTemplatesOnly:
    """Catalog-shaped stub exposing only a prebuilt templates list."""

    def __init__(self, templates: list[object]) -> None:
        self.templates = templates


class _RefNoRole:
    """RequirementRef-shaped stub without ``role`` (old code defaulted None)."""

    template_id = "T1"


class _SpecRefNoRole:
    """TaskSpec-shaped stub whose single criterion dropped ``role``."""

    criteria = [_RefNoRole()]


class _TmplNoRole:
    """RequirementTemplate-shaped stub without ``role`` (old code defaulted None)."""

    id = "T1"
    predicts_null = False


class _RefToT1:
    """RequirementRef-shaped stub resolving to ``_TmplNoRole``."""

    template_id = "T1"
    role = None


class _SpecRefToT1:
    """TaskSpec-shaped stub whose single criterion resolves to ``_TmplNoRole``."""

    criteria = [_RefToT1()]


class _TmplNoPredictsNull:
    """RequirementTemplate stub without ``predicts_null`` (old code defaulted False)."""

    id = "T2"
    role = "validity"


class _RefToT2:
    """RequirementRef-shaped stub resolving to ``_TmplNoPredictsNull``."""

    template_id = "T2"
    role = None


class _SpecRefToT2:
    """TaskSpec-shaped stub whose single criterion resolves to ``_TmplNoPredictsNull``."""

    criteria = [_RefToT2()]


class _SpecNoTaskKey:
    """TaskSpec-shaped stub without ``task_key`` (old code defaulted None)."""

    addresses: list[str] = []


class _TaskWithSpecNoTaskKey:
    """Task-shaped stub whose spec dropped ``task_key``."""

    state = TaskState.PENDING
    spec = _SpecNoTaskKey()


class _RuntimeNoStudy:
    """Runtime stub with a real graph slot and no ``study`` slot (old defaulted None)."""

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
    # surface is required before criteria are inspected.
    with pytest.raises(
        AttributeError,
        match=re.escape("'_RuntimeCoreNone' object has no attribute 'catalog'"),
    ):
        task_protected_from_agent_skip(_RuntimeCoreNone(), _TaskWithSpecNoCriteria())


def test_task_links_requires_task_spec_attribute() -> None:
    # Was: the spec probe fell back to the task itself and a missing
    # addresses attribute silently meant "not linked". Now: the spec link is
    # required.
    with pytest.raises(
        AttributeError,
        match=re.escape("'_TaskNoSpec' object has no attribute 'spec'"),
    ):
        _task_links_to_target(_RuntimeCoreNone(), _TaskNoSpec())


def test_refute_tests_edge_requires_task_spec_attribute() -> None:
    # Was: the nested task_key probe defaulted silently to None and the
    # refute tests edge was treated as absent. Now: the spec link is
    # required (study/graph are present here, so the key line is reached).
    with pytest.raises(
        AttributeError,
        match=re.escape("'_TaskNoSpec' object has no attribute 'spec'"),
    ):
        _has_refute_tests_edge(_RuntimeStudyGraphOnly(), _TaskNoSpec())


def test_should_skip_requires_task_addresses_attribute() -> None:
    # Was: a missing ``addresses`` attribute defaulted to an empty set and the
    # task was silently kept. Now: the addresses field is required.
    with pytest.raises(
        AttributeError,
        match=re.escape("'_SpecNoAddresses' object has no attribute 'addresses'"),
    ):
        should_skip_for_question_supersede(
            task=_SpecNoAddresses(),
            superseded_old={"Q1"},
            active_q_after=set(),
            protected_keys=set(),
        )


def test_criteria_prediction_requires_ref_role_attribute() -> None:
    # Was: a criterion without ``role`` defaulted to None and the task
    # silently lost prediction protection. Now: the ref role is required.
    runtime = _RuntimeCatalogOnly(Catalog(catalog_version=1, edition="test"))
    with pytest.raises(
        AttributeError,
        match=re.escape("'_RefNoRole' object has no attribute 'role'"),
    ):
        _criteria_prediction_capable(runtime, _SpecRefNoRole())


def test_criteria_prediction_requires_template_role_attribute() -> None:
    # Was: a catalog template without ``role`` defaulted to None and the task
    # silently lost prediction protection. Now: the template role is required.
    runtime = _RuntimeCatalogOnly(_CatalogTemplatesOnly([_TmplNoRole()]))
    with pytest.raises(
        AttributeError,
        match=re.escape("'_TmplNoRole' object has no attribute 'role'"),
    ):
        _criteria_prediction_capable(runtime, _SpecRefToT1())


def test_criteria_prediction_requires_predicts_null_attribute() -> None:
    # Was: a template without ``predicts_null`` defaulted to False and
    # null-predicting templates were silently ignored. Now: the flag is
    # required on the resolved template.
    runtime = _RuntimeCatalogOnly(_CatalogTemplatesOnly([_TmplNoPredictsNull()]))
    with pytest.raises(
        AttributeError,
        match=re.escape("'_TmplNoPredictsNull' object has no attribute 'predicts_null'"),
    ):
        _criteria_prediction_capable(runtime, _SpecRefToT2())


def test_task_links_requires_spec_task_key_attribute() -> None:
    # Was: a spec without ``task_key`` defaulted to None and the experiment
    # tests-edge lookup was silently skipped. Now: the spec key is required.
    with pytest.raises(
        AttributeError,
        match=re.escape("'_SpecNoTaskKey' object has no attribute 'task_key'"),
    ):
        _task_links_to_target(_RuntimeStudyGraphOnly(), _TaskWithSpecNoTaskKey())


def test_refute_tests_edge_requires_study_attribute() -> None:
    # Was: a runtime without ``study`` defaulted to None and the refute tests
    # edge was silently treated as absent. Now: the study slot is required.
    with pytest.raises(
        AttributeError,
        match=re.escape("'_RuntimeNoStudy' object has no attribute 'study'"),
    ):
        _has_refute_tests_edge(_RuntimeNoStudy(), _TaskNoSpec())
