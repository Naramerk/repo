"""ResearchSpec typed models (CAPABILITIES #1 / DESIGN §7.5).

Role ``tz_spec`` returns a schema-valid ``ResearchSpec``. Graph projection
(``ResearchSpecOut``, questions) lands in a follow-up PR. No runner here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SpecModel(BaseModel):
    """Frozen, strict, no extras — agent outputs must match the schema exactly."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


ConstraintSubtype = Literal["resource", "data", "norm", "ethics", "domain", "profile"]
MetricOp = Literal["<", "<=", "==", ">=", ">", "in"]
MetricCheck = Literal["code", "review", "rubric"]


class Goal(SpecModel):
    """Study goal (RESEARCH_MODEL ``study.goal`` / graph_schema Goal)."""

    statement: str = Field(min_length=1)
    research_form: str | None = None
    trl: int | None = None


class Constraint(SpecModel):
    """Study constraint; node only when measured_by points at it."""

    subtype: ConstraintSubtype
    content: str = Field(min_length=1)
    limit: str | None = None


class SuccessMetric(SpecModel):
    """Study-level success criterion (maps to criterion nodes from SPEC)."""

    metric: str = Field(min_length=1)
    op: MetricOp
    target: str = Field(min_length=1)
    check: MetricCheck
    tolerance: float | None = None


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
