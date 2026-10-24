"""ResearchSpec typed models — CAPABILITIES #1 field surface (no runner)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.agents.research_spec import (
    Constraint,
    Goal,
    ResearchSpec,
    SuccessMetric,
)


def _goal(**over: object) -> Goal:
    base: dict[str, object] = {"statement": "measure X under Y"}
    base.update(over)
    return Goal.model_validate(base)


def _constraint(**over: object) -> Constraint:
    base: dict[str, object] = {"subtype": "resource", "content": "max 10 LLM calls"}
    base.update(over)
    return Constraint.model_validate(base)


def _metric(**over: object) -> SuccessMetric:
    base: dict[str, object] = {
        "metric": "sources",
        "op": ">=",
        "target": "5",
        "check": "code",
    }
    base.update(over)
    return SuccessMetric.model_validate(base)


def _spec(**over: object) -> ResearchSpec:
    base: dict[str, object] = {
        "object": "catalyst X",
        "goal": _goal(),
        "scope": "peer-reviewed literature 2015+",
        "constraints": (_constraint(),),
        "success_metrics": (_metric(),),
    }
    base.update(over)
    return ResearchSpec.model_validate(base)


def test_research_spec_accepts_cap1_fields() -> None:
    spec = _spec()
    assert spec.object == "catalyst X"
    assert spec.goal.statement == "measure X under Y"
    assert spec.scope.startswith("peer-reviewed")
    assert len(spec.constraints) == 1
    assert len(spec.success_metrics) == 1


def test_research_spec_round_trip_json() -> None:
    spec = _spec(
        goal=_goal(research_form="empirical", trl=3),
        constraints=(
            _constraint(subtype="ethics", content="no human subjects", limit="n/a"),
        ),
        success_metrics=(
            _metric(metric="coverage", op=">=", target="0.8", check="review", tolerance=0.01),
        ),
    )
    restored = ResearchSpec.model_validate(spec.model_dump(mode="json"))
    assert restored == spec


def test_research_spec_frozen() -> None:
    spec = _spec()
    with pytest.raises(ValidationError):
        spec.object = "other"  # type: ignore[misc]


def test_research_spec_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        ResearchSpec.model_validate(
            {
                "object": "x",
                "goal": {"statement": "g"},
                "scope": "s",
                "constraints": [],
                "success_metrics": [],
                "extra": True,
            }
        )


def test_research_spec_rejects_empty_object() -> None:
    with pytest.raises(ValidationError):
        _spec(object="")


def test_research_spec_rejects_empty_scope() -> None:
    with pytest.raises(ValidationError):
        _spec(scope="")


def test_goal_rejects_blank_statement() -> None:
    with pytest.raises(ValidationError):
        _goal(statement="")


def test_goal_optional_research_form_and_trl() -> None:
    g = _goal()
    assert g.research_form is None
    assert g.trl is None


def test_constraint_rejects_unknown_subtype() -> None:
    with pytest.raises(ValidationError):
        _constraint(subtype="budget")


def test_constraint_rejects_blank_content() -> None:
    with pytest.raises(ValidationError):
        _constraint(content="")


def test_success_metric_rejects_unknown_op() -> None:
    with pytest.raises(ValidationError):
        _metric(op="~=")


def test_success_metric_rejects_unknown_check() -> None:
    with pytest.raises(ValidationError):
        _metric(check="llm")


def test_success_metric_rejects_blank_metric_or_target() -> None:
    with pytest.raises(ValidationError):
        _metric(metric="")
    with pytest.raises(ValidationError):
        _metric(target="")


def test_research_spec_allows_empty_constraints_and_metrics() -> None:
    """Emptiness policy is for validate_research_spec (later PR), not the type."""
    spec = _spec(constraints=(), success_metrics=())
    assert spec.constraints == ()
    assert spec.success_metrics == ()


def test_research_spec_strict_rejects_coercion() -> None:
    with pytest.raises(ValidationError):
        SuccessMetric.model_validate(
            {"metric": "n", "op": ">=", "target": "1", "check": "code", "tolerance": "0.1"}
        )
