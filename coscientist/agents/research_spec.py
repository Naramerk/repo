"""ResearchSpec typed models (CAPABILITIES #1 / DESIGN §7.5).

``tz_spec`` returns a schema-valid ``ResearchSpec`` via ``AgentPort.run``
(after G1). This module is types only — **no Protocol / port**.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SpecModel(BaseModel):
    """Frozen, strict, no extras — SPEC output must match the schema exactly."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


ConstraintSubtype = Literal["resource", "data", "norm", "ethics", "domain", "profile"]
MetricOp = Literal["<", "<=", "==", ">=", ">", "in"]
MetricCheck = Literal["code", "review", "rubric"]


class Goal(SpecModel):
    """Study goal (RESEARCH_MODEL ``study.goal`` / graph_schema Goal)."""

    statement: str = Field(min_length=1)
    research_form: str | None = None  # audit-ok[field_default]: graph_schema Goal.research_form optional
    trl: int | None = None  # audit-ok[field_default]: graph_schema Goal.trl optional


class Constraint(SpecModel):
    """Study constraint; node only when measured_by points at it."""

    subtype: ConstraintSubtype
    content: str = Field(min_length=1)
    limit: str | None = None  # audit-ok[field_default]: graph_schema constraint.limit optional


class SuccessMetric(SpecModel):
    """Study-level success criterion (maps to criterion nodes from SPEC)."""

    metric: str = Field(min_length=1)
    op: MetricOp
    target: str = Field(min_length=1)
    check: MetricCheck
    tolerance: float | None = None  # audit-ok[field_default]: graph_schema criterion.tolerance optional


class ResearchSpec(SpecModel):
    """SPEC role output (CAPABILITIES #1; DESIGN §7.5).

    Fields: object, goal, scope, constraints, success_metrics.
    """

    object: str = Field(min_length=1)
    goal: Goal
    scope: str = Field(min_length=1)
    constraints: tuple[Constraint, ...]
    success_metrics: tuple[SuccessMetric, ...]

    @field_validator("constraints", "success_metrics", mode="before")
    @classmethod
    def _json_list_to_tuple(cls, v: Any) -> Any:
        """JSON/YAML lists become tuples; other types stay for strict checking."""
        if isinstance(v, list):
            return tuple(v)
        return v
