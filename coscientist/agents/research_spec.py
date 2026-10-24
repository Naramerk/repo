"""ResearchSpec / ResearchSpecOut typed models (CAPABILITIES #1 / DESIGN §7.5).

Role ``tz_spec`` returns schema-valid ``ResearchSpec``; graph-facing
``ResearchSpecOut`` adds ``questions[]`` (≥1). No runner here.
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
FinalTypeName = Literal["verdict", "artifact", "both"]


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


class SpecQuestion(SpecModel):
    """Research question from SPEC (graph_schema ``question`` / ResearchSpecOut)."""

    formulation: str = Field(min_length=1)
    id: str | None = None  # audit-ok[field_default]: question id assigned later if omitted
    gap: str | None = None  # audit-ok[field_default]: graph_schema question.gap optional
    scope: str | None = None  # audit-ok[field_default]: graph_schema question.scope optional
    final_type: FinalTypeName | None = None  # audit-ok[field_default]: final_type defaulted at Plan.accept
    parent_id: str | None = None  # audit-ok[field_default]: graph_schema question parent optional


class ResearchSpecOut(ResearchSpec):
    """Graph-facing SPEC output: ResearchSpec fields + ``questions[]`` (≥1)."""

    questions: tuple[SpecQuestion, ...] = Field(min_length=1)

    @field_validator("questions", mode="before")
    @classmethod
    def _questions_list_to_tuple(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v


def spec_out_question_dicts(out: ResearchSpecOut) -> list[dict[str, Any]]:
    """Payload for ``questions_from_spec_output`` / ``accept_plan(..., questions=)``.

    Keeps ``formulation`` (and optional ``id`` / ``final_type`` / ``parent_id``);
    core adapter derives ``text`` and default ids.
    """
    rows: list[dict[str, Any]] = []
    for q in out.questions:
        row: dict[str, Any] = {"formulation": q.formulation}
        if q.id is not None:
            row["id"] = q.id
        if q.final_type is not None:
            row["final_type"] = q.final_type
        if q.parent_id is not None:
            row["parent_id"] = q.parent_id
        if q.gap is not None:
            row["gap"] = q.gap
        if q.scope is not None:
            row["scope"] = q.scope
        rows.append(row)
    return rows
