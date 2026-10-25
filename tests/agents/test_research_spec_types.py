"""ResearchSpec typed models — CAPABILITIES #1 field surface (no runner)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.agents.research_spec import (
    Constraint,
    Goal,
    ResearchSpec,
    SuccessMetric,
)


def _assert_err(exc: ValidationError, *, loc: tuple[Any, ...], err_type: str) -> None:
    (err,) = exc.errors()
    assert err["loc"] == loc
    assert err["type"] == err_type


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
    assert spec.goal == Goal(statement="measure X under Y", research_form=None, trl=None)
    assert spec.scope == "peer-reviewed literature 2015+"
    assert spec.constraints == (Constraint(subtype="resource", content="max 10 LLM calls", limit=None),)
    assert spec.success_metrics == (SuccessMetric(metric="sources", op=">=", target="5", check="code", tolerance=None),)


def test_research_spec_round_trip_json() -> None:
    spec = _spec(
        goal=_goal(research_form="empirical", trl=3),
        constraints=(_constraint(subtype="ethics", content="no human subjects", limit="n/a"),),
        success_metrics=(
            _metric(
                metric="coverage",
                op=">=",
                target="0.8",
                check="review",
                tolerance=0.01,
            ),
        ),
    )
    restored = ResearchSpec.model_validate_json(spec.model_dump_json())
    assert restored == spec


def test_research_spec_frozen() -> None:
    spec = _spec()
    with pytest.raises(ValidationError) as ei:
        spec.object = "other"  # type: ignore[misc]
    _assert_err(ei.value, loc=("object",), err_type="frozen_instance")


def test_research_spec_rejects_extra_field() -> None:
    with pytest.raises(ValidationError) as ei:
        ResearchSpec.model_validate(
            {
                "object": "x",
                "goal": {"statement": "g"},
                "scope": "s",
                "constraints": (),
                "success_metrics": (),
                "extra": True,
            }
        )
    _assert_err(ei.value, loc=("extra",), err_type="extra_forbidden")


@pytest.mark.parametrize("blank", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
def test_research_spec_rejects_blank_object(blank: str) -> None:
    with pytest.raises(ValidationError) as ei:
        _spec(object=blank)
    _assert_err(ei.value, loc=("object",), err_type="string_pattern_mismatch")


@pytest.mark.parametrize("blank", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
def test_research_spec_rejects_blank_scope(blank: str) -> None:
    with pytest.raises(ValidationError) as ei:
        _spec(scope=blank)
    _assert_err(ei.value, loc=("scope",), err_type="string_pattern_mismatch")


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "spaces"])
def test_goal_rejects_blank_statement(blank: str) -> None:
    with pytest.raises(ValidationError) as ei:
        _goal(statement=blank)
    _assert_err(ei.value, loc=("statement",), err_type="string_pattern_mismatch")


def test_goal_optional_research_form_and_trl() -> None:
    g = _goal()
    assert g == Goal(statement="measure X under Y", research_form=None, trl=None)


def test_constraint_rejects_unknown_subtype() -> None:
    with pytest.raises(ValidationError) as ei:
        _constraint(subtype="budget")
    _assert_err(ei.value, loc=("subtype",), err_type="literal_error")


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "spaces"])
def test_constraint_rejects_blank_content(blank: str) -> None:
    with pytest.raises(ValidationError) as ei:
        _constraint(content=blank)
    _assert_err(ei.value, loc=("content",), err_type="string_pattern_mismatch")


def test_success_metric_rejects_unknown_op() -> None:
    with pytest.raises(ValidationError) as ei:
        _metric(op="~=")
    _assert_err(ei.value, loc=("op",), err_type="literal_error")


def test_success_metric_rejects_unknown_check() -> None:
    with pytest.raises(ValidationError) as ei:
        _metric(check="llm")
    _assert_err(ei.value, loc=("check",), err_type="literal_error")


@pytest.mark.parametrize(
    ("field", "blank"),
    [("metric", ""), ("metric", "   "), ("target", ""), ("target", "   ")],
    ids=["metric-empty", "metric-spaces", "target-empty", "target-spaces"],
)
def test_success_metric_rejects_blank_metric_or_target(field: str, blank: str) -> None:
    with pytest.raises(ValidationError) as ei:
        _metric(**{field: blank})
    _assert_err(ei.value, loc=(field,), err_type="string_pattern_mismatch")


def test_research_spec_allows_empty_constraints_and_metrics() -> None:
    """Emptiness policy is for validate_research_spec (later PR), not the type."""
    spec = _spec(constraints=(), success_metrics=())
    assert spec.constraints == ()
    assert spec.success_metrics == ()


def test_success_metric_strict_rejects_tolerance_string() -> None:
    with pytest.raises(ValidationError) as ei:
        SuccessMetric.model_validate(
            {
                "metric": "n",
                "op": ">=",
                "target": "1",
                "check": "code",
                "tolerance": "0.1",
            }
        )
    _assert_err(ei.value, loc=("tolerance",), err_type="float_type")


def test_success_metric_rejects_negative_tolerance() -> None:
    with pytest.raises(ValidationError) as ei:
        _metric(tolerance=-0.1)
    _assert_err(ei.value, loc=("tolerance",), err_type="greater_than_equal")


def test_success_metric_rejects_nan_tolerance() -> None:
    with pytest.raises(ValidationError) as ei:
        _metric(tolerance=float("nan"))
    _assert_err(ei.value, loc=("tolerance",), err_type="finite_number")


def test_goal_strict_rejects_trl_string() -> None:
    with pytest.raises(ValidationError) as ei:
        _goal(trl="3")
    _assert_err(ei.value, loc=("trl",), err_type="int_type")


def test_goal_strict_rejects_trl_bool() -> None:
    with pytest.raises(ValidationError) as ei:
        _goal(trl=True)
    _assert_err(ei.value, loc=("trl",), err_type="int_type")


def test_research_spec_strict_rejects_goal_as_string() -> None:
    with pytest.raises(ValidationError) as ei:
        ResearchSpec.model_validate(
            {
                "object": "x",
                "goal": "measure X",
                "scope": "s",
                "constraints": (),
                "success_metrics": (),
            }
        )
    _assert_err(ei.value, loc=("goal",), err_type="model_type")


def test_research_spec_json_round_trip_equality() -> None:
    raw = (
        b'{"object":"x","goal":{"statement":"g","research_form":null,"trl":null},'
        b'"scope":"s","constraints":[{"subtype":"data","content":"csv only",'
        b'"limit":null}],"success_metrics":[{"metric":"n","op":"==","target":"1",'
        b'"check":"code","tolerance":null}]}'
    )
    spec = ResearchSpec.model_validate_json(raw)
    assert spec == ResearchSpec(
        object="x",
        goal=Goal(statement="g", research_form=None, trl=None),
        scope="s",
        constraints=(Constraint(subtype="data", content="csv only", limit=None),),
        success_metrics=(SuccessMetric(metric="n", op="==", target="1", check="code", tolerance=None),),
    )
    assert ResearchSpec.model_validate_json(spec.model_dump_json()) == spec
