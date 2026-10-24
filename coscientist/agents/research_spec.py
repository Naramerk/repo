"""ResearchSpec typed models (CAPABILITIES #1 / DESIGN §7.5).

``tz_spec`` returns a schema-valid ``ResearchSpec`` via ``AgentPort.run``
(after G1). This module is types only — **no Protocol / port**.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class SpecModel(BaseModel):
    """Frozen, strict, no extras — SPEC output must match the schema exactly."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


ConstraintSubtype = Literal["resource", "data", "norm", "ethics", "domain", "profile"]
MetricOp = Literal["<", "<=", "==", ">=", ">", "in"]
MetricCheck = Literal["code", "review", "rubric"]

# At least one non-whitespace character (rejects "" and "   "; spaces elsewhere OK).
_NONBLANK = r".*\S.*"


class Goal(SpecModel):
    """Study goal (RESEARCH_MODEL ``study.goal`` / graph_schema Goal)."""

    statement: str = Field(pattern=_NONBLANK)
    research_form: str | None = None  # audit-ok[field_default]: graph_schema Goal.research_form optional
    trl: int | None = None  # audit-ok[field_default]: graph_schema Goal.trl optional


class Constraint(SpecModel):
    """Study constraint; node only when measured_by points at it."""

    subtype: ConstraintSubtype
    content: str = Field(pattern=_NONBLANK)
    limit: str | None = None  # audit-ok[field_default]: graph_schema constraint.limit optional


class SuccessMetric(SpecModel):
    """Study-level success criterion (maps to criterion nodes from SPEC)."""

    metric: str = Field(pattern=_NONBLANK)
    op: MetricOp
    target: str = Field(pattern=_NONBLANK)
    check: MetricCheck
    tolerance: Annotated[float | None, Field(ge=0, allow_inf_nan=False)] = None  # audit-ok[field_default]: graph_schema criterion.tolerance optional


class ResearchSpec(SpecModel):
    """SPEC role output (CAPABILITIES #1; DESIGN §7.5).

    Fields: object, goal, scope, constraints, success_metrics.
    """

    object: str = Field(pattern=_NONBLANK)
    goal: Goal
    scope: str = Field(pattern=_NONBLANK)
    constraints: tuple[Constraint, ...]
    success_metrics: tuple[SuccessMetric, ...]
