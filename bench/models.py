"""Benchmark harness models (plan items 3–4).

Item 3: task / quality expectation / manifest (required fields only).
Item 4: runner/suite results — provider ``Usage`` + per-call ``UsageRecorded``
payloads for LLM-call accounting (no harness ``CallCounts``).
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from coscientist.llm.types import Usage


class EvidenceOutcomeCount(BaseModel):
    """One evidence-outcome tally as declared in task YAML (e.g. success:2)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: str = Field(min_length=1)
    count: StrictInt = Field(ge=0)


class QualityExpectation(BaseModel):
    """Exact-compare targets declared in task data (plan item 8; no scores)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    status: str = Field(min_length=1)
    exp_tasks: StrictInt = Field(ge=0)
    evidence: tuple[EvidenceOutcomeCount, ...]

    @model_validator(mode="after")
    def _unique_evidence_outcomes(self) -> QualityExpectation:
        outcomes = [row.outcome for row in self.evidence]
        if len(outcomes) != len(set(outcomes)):
            raise ValueError("QualityExpectation.evidence outcomes must be unique")
        return self


class BenchTask(BaseModel):
    """One research scenario loaded from a versioned YAML task file."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    task_id: str = Field(min_length=1)
    task_set_version: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    quality: QualityExpectation


def _require_relative_task_path(path: str) -> None:
    """Refuse empty/whitespace, absolute, or ``..``-escaping task paths."""
    if not path or path != path.strip() or any(ch.isspace() for ch in path):
        raise ValueError("TaskSetManifest.tasks entries must be non-empty paths without whitespace")
    parsed = PurePosixPath(path)
    if parsed.is_absolute():
        raise ValueError(f"TaskSetManifest.tasks entries must be relative paths (got absolute: {path!r})")
    if ".." in parsed.parts:
        raise ValueError(f"TaskSetManifest.tasks entries must not contain '..' (got {path!r})")


class TaskSetManifest(BaseModel):
    """Index of relative task YAML paths for a versioned set."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    task_set_id: str = Field(min_length=1)
    task_set_version: str = Field(min_length=1)
    tasks: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _relative_unique_task_paths(self) -> TaskSetManifest:
        for path in self.tasks:
            _require_relative_task_path(path)
        if len(self.tasks) != len(set(self.tasks)):
            raise ValueError("TaskSetManifest.tasks paths must be unique")
        return self


class UsageRecorded(BaseModel):
    """One LLM-call usage record (DESIGN §10 ``UsageRecorded`` payload fields).

    The production journal event type is owned by scheduler S1
    ([#129](https://github.com/Naramerk/repo/issues/129)). This model is the
    harness accounting shape adapters/fakes supply; it does not append events.
    Tokens come only from nested provider ``Usage`` (never estimated).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    study_id: str = Field(min_length=1)
    task_id: str | None
    attempt_id: str | None
    role: str = Field(min_length=1)
    usage: Usage


class QualityObservation(BaseModel):
    """Quality signals reported by a runner (exact-compare fields + record_result)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    status: str = Field(min_length=1)
    exp_tasks: StrictInt = Field(ge=0)
    evidence: tuple[EvidenceOutcomeCount, ...]
    record_result: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _unique_evidence_outcomes(self) -> QualityObservation:
        outcomes = [row.outcome for row in self.evidence]
        if len(outcomes) != len(set(outcomes)):
            raise ValueError("QualityObservation.evidence outcomes must be unique")
        return self


class RunnerSuccess(BaseModel):
    """Successful runner payload before harness wall-time attachment."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["success"]
    usage_recorded: tuple[UsageRecorded, ...]
    quality: QualityObservation


class RunnerRefuse(BaseModel):
    """Typed refusal from a runner or adapter (no silent skip)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["refused"]
    reason_code: str = Field(min_length=1)
    detail: str = Field(min_length=1)


RunnerRawResult = RunnerSuccess | RunnerRefuse


class TimedRunOk(BaseModel):
    """One timed successful run (HER-comparable metrics derive from this)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["ok"]
    system_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    dur_s: float = Field(ge=0.0)
    usage_recorded: tuple[UsageRecorded, ...]
    quality: QualityObservation


class TimedRunRefused(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["refused"]
    system_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    dur_s: float = Field(ge=0.0)
    reason_code: str = Field(min_length=1)
    detail: str = Field(min_length=1)


TimedRunResult = TimedRunOk | TimedRunRefused


class TaskComparison(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    task_id: str = Field(min_length=1)
    old: TimedRunResult
    new: TimedRunResult

    @model_validator(mode="after")
    def _task_ids_match(self) -> TaskComparison:
        if self.old.task_id != self.task_id or self.new.task_id != self.task_id:
            raise ValueError(
                "TaskComparison.task_id must equal old.task_id and new.task_id "
                f"(got task_id={self.task_id!r}, old={self.old.task_id!r}, "
                f"new={self.new.task_id!r})"
            )
        return self


class BenchSuiteResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    task_set_id: str = Field(min_length=1)
    task_set_version: str = Field(min_length=1)
    comparisons: tuple[TaskComparison, ...]


def llm_call_count(records: tuple[UsageRecorded, ...]) -> int:
    """LLM calls = number of journaled ``UsageRecorded`` payloads (not estimated)."""
    return len(records)


def token_total(records: tuple[UsageRecorded, ...]) -> int:
    """Sum of provider prompt+completion tokens across recorded calls."""
    return sum(r.usage.prompt_tokens + r.usage.completion_tokens for r in records)
