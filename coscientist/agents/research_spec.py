"""ResearchSpec typed models (CAPABILITIES #1 / DESIGN §7.5).

``tz_spec`` returns a schema-valid ``ResearchSpec`` via ``AgentPort.run``
(after G1). This module is types only — **no Protocol / port**.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import Field, model_validator

from coscientist.agents.strict import NONBLANK, AgentModel

ConstraintSubtype = Literal["resource", "data", "norm", "ethics", "domain", "profile"]
MetricOp = Literal["<", "<=", "==", ">=", ">", "in"]
MetricCheck = Literal["code", "review", "rubric"]


def _pad_absent(data: object, defaults: Mapping[str, object]) -> object:
    """Fill keys omitted on historical / partial JSON (wire absences → explicit values)."""
    if not isinstance(data, dict):
        return data
    missing = {key: value for key, value in defaults.items() if key not in data}
    if not missing:
        return data
    return {**data, **missing}


class Goal(AgentModel):
    """Study goal (RESEARCH_MODEL ``study.goal`` / graph_schema Goal)."""

    statement: str = Field(pattern=NONBLANK)
    # graph_schema Goal.research_form / trl optional — absent on historical SPEC JSON.
    research_form: str | None
    trl: int | None

    @model_validator(mode="before")
    @classmethod
    def _absent_optional_goal_fields(cls, data: object) -> object:
        return _pad_absent(data, {"research_form": None, "trl": None})


class Constraint(AgentModel):
    """Study constraint; node only when measured_by points at it."""

    subtype: ConstraintSubtype
    content: str = Field(pattern=NONBLANK)
    # graph_schema constraint.limit optional — absent when unconstrained.
    limit: str | None

    @model_validator(mode="before")
    @classmethod
    def _absent_optional_limit(cls, data: object) -> object:
        return _pad_absent(data, {"limit": None})


class SuccessMetric(AgentModel):
    """Study-level success criterion (maps to criterion nodes from SPEC)."""

    metric: str = Field(pattern=NONBLANK)
    op: MetricOp
    target: str = Field(pattern=NONBLANK)
    check: MetricCheck
    # Optional SESOI-style tolerance — absent when the metric has no band.
    tolerance: Annotated[float | None, Field(ge=0, allow_inf_nan=False)]

    @model_validator(mode="before")
    @classmethod
    def _absent_optional_tolerance(cls, data: object) -> object:
        return _pad_absent(data, {"tolerance": None})


class ResearchSpec(AgentModel):
    """SPEC role output (CAPABILITIES #1; DESIGN §7.5).

    Fields: object, goal, scope, constraints, success_metrics.
    """

    object: str = Field(pattern=NONBLANK)
    goal: Goal
    scope: str = Field(pattern=NONBLANK)
    constraints: tuple[Constraint, ...]
    success_metrics: tuple[SuccessMetric, ...]
