"""Typed models for tasks, runner results, and comparison output.

All operational values come from config or from runner-reported provider
usage. No estimated tokens, no inferred quality from names/shape.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanCriterionExpectation(BaseModel):
    """One plan criterion the task set requires the system to meet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_id: str = Field(min_length=1)
    required_status: str = Field(min_length=1)


class QualityExpectation(BaseModel):
    """Quality checks declared in the task config (not inferred)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    required_report_fields: tuple[str, ...]
    plan_criteria: tuple[PlanCriterionExpectation, ...]


class BenchTask(BaseModel):
    """One research task loaded from a versioned YAML file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    task_set_version: str = Field(min_length=1)
    title: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    quality: QualityExpectation


class TaskSetManifest(BaseModel):
    """Index of task files for a versioned set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_set_id: str = Field(min_length=1)
    task_set_version: str = Field(min_length=1)
    tasks: tuple[str, ...] = Field(min_length=1)


class ProviderTokenUsage(BaseModel):
    """Token counts taken from provider usage accounting only.

    ``total_tokens`` must equal ``prompt_tokens + completion_tokens``. The
    harness refuses inconsistent provider reports rather than correcting them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def _total_matches_parts(self) -> ProviderTokenUsage:
        expected = self.prompt_tokens + self.completion_tokens
        if self.total_tokens != expected:
            raise ValueError(
                "total_tokens must equal prompt_tokens + completion_tokens "
                f"(got total={self.total_tokens}, sum={expected})"
            )
        return self


class CallCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    llm_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)


class VerdictObservation(BaseModel):
    """A question disposition as reported by the system under test."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str = Field(min_length=1)
    disposition: str = Field(min_length=1)


class PlanCriterionObservation(BaseModel):
    """Plan criterion status as reported by the system under test."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_id: str = Field(min_length=1)
    status: str = Field(min_length=1)


class QualityObservation(BaseModel):
    """Structured quality signals reported by the runner (not guessed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: tuple[VerdictObservation, ...]
    plan_criteria: tuple[PlanCriterionObservation, ...]
    report_fields_present: tuple[str, ...]


class RunnerSuccess(BaseModel):
    """Successful runner payload before harness wall-time attachment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["success"]
    tokens: ProviderTokenUsage
    calls: CallCounts
    quality: QualityObservation


class RunnerRefuse(BaseModel):
    """Typed refusal from a runner or adapter (no silent skip)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["refused"]
    reason_code: str = Field(min_length=1)
    detail: str = Field(min_length=1)


RunnerRawResult = RunnerSuccess | RunnerRefuse


class QualityScore(BaseModel):
    """Explicit tallies against task ``QualityExpectation`` (no heuristics)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_fields_required: int = Field(ge=0)
    report_fields_present: int = Field(ge=0)
    report_fields_missing: tuple[str, ...]
    plan_criteria_required: int = Field(ge=0)
    plan_criteria_met: int = Field(ge=0)
    plan_criteria_unmet: tuple[str, ...]
    verdict_count: int = Field(ge=0)


class TimedRunOk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["ok"]
    system_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    wall_time_ms: float = Field(ge=0.0)
    tokens: ProviderTokenUsage
    calls: CallCounts
    quality: QualityObservation
    quality_score: QualityScore


class TimedRunRefused(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["refused"]
    system_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    wall_time_ms: float = Field(ge=0.0)
    reason_code: str = Field(min_length=1)
    detail: str = Field(min_length=1)


TimedRunResult = TimedRunOk | TimedRunRefused


class TaskComparison(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    old: TimedRunResult
    new: TimedRunResult


class BenchSuiteResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_set_id: str = Field(min_length=1)
    task_set_version: str = Field(min_length=1)
    comparisons: tuple[TaskComparison, ...]
