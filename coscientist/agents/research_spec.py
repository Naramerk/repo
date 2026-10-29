"""ResearchSpec typed models (CAPABILITIES #1 / DESIGN §7.5).

``tz_spec`` returns a schema-valid ``ResearchSpec`` via ``AgentPort.run``
(after G1). This module is types only — **no Protocol / port**.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from coscientist.agents.strict import NONBLANK, AgentModel

ConstraintSubtype = Literal["resource", "data", "norm", "ethics", "domain", "profile"]
MetricOp = Literal["<", "<=", "==", ">=", ">", "in"]
MetricCheck = Literal["code", "review", "rubric"]


class Goal(AgentModel):
    """Study goal (RESEARCH_MODEL ``study.goal`` / graph_schema Goal)."""

    statement: str = Field(pattern=NONBLANK)
    research_form: str | None = Field(default=None)  # graph_schema Goal.research_form optional
    trl: int | None = Field(default=None)  # graph_schema Goal.trl optional


class Constraint(AgentModel):
    """Study constraint; node only when measured_by points at it."""

    subtype: ConstraintSubtype
    content: str = Field(pattern=NONBLANK)
    limit: str | None = Field(default=None)  # graph_schema constraint.limit optional


class SuccessMetric(AgentModel):
    """Study-level success criterion (maps to criterion nodes from SPEC)."""

    metric: str = Field(pattern=NONBLANK)
    op: MetricOp
    target: str = Field(pattern=NONBLANK)
    check: MetricCheck
    tolerance: Annotated[
        float | None, Field(default=None, ge=0, allow_inf_nan=False)
    ]  # optional SESOI-style tolerance


class ResearchSpec(AgentModel):
    """SPEC role output (CAPABILITIES #1; DESIGN §7.5).

    Fields: object, goal, scope, constraints, success_metrics.
    """

    object: str = Field(pattern=NONBLANK)
    goal: Goal
    scope: str = Field(pattern=NONBLANK)
    constraints: tuple[Constraint, ...]
    success_metrics: tuple[SuccessMetric, ...]
