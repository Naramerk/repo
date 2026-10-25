"""Benchmark harness models (plan items 3–4).

Item 3: task / quality expectation / manifest (required fields only).
Item 4: runner/suite results — provider ``Usage`` + per-call ``UsageRecorded``
payloads for LLM-call accounting (no harness ``CallCounts``).
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from coscientist.llm.types import Usage

TaskDriver = Literal["web", "recovery"]


class EvidenceOutcomeCount(BaseModel):
    """One evidence-outcome tally as declared in task YAML (e.g. success:2)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: str = Field(min_length=1)
    count: StrictInt = Field(ge=0)


class QualityBase(BaseModel):
    """Shared status / exp_tasks / evidence for expected and observed quality."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    status: str = Field(min_length=1)
    exp_tasks: StrictInt = Field(ge=0)
    evidence: tuple[EvidenceOutcomeCount, ...]

    @model_validator(mode="after")
    def _unique_evidence_outcomes(self) -> Self:
        outcomes = [row.outcome for row in self.evidence]
        if len(outcomes) != len(set(outcomes)):
            raise ValueError(f"{type(self).__name__}.evidence outcomes must be unique")
        return self


class QualityExpectation(QualityBase):
    """Exact-compare targets declared in task data (plan item 8; no scores)."""


class QualityObservation(QualityBase):
    """Runner-reported quality: shared fields plus ``record_result`` count."""

    record_result: StrictInt = Field(ge=0)


class QualityComparison(BaseModel):
    """Exact-equality result of observed vs task-expected quality (plan item 8).

    Field booleans are the only outcome — no scores, weights, or fractions.
    ``evidence_equal`` is order-sensitive tuple equality.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    task_id: str = Field(min_length=1)
    status_equal: bool
    exp_tasks_equal: bool
    evidence_equal: bool
    equal: bool

    @model_validator(mode="after")
    def _equal_is_conjunction(self) -> Self:
        computed = self.status_equal and self.exp_tasks_equal and self.evidence_equal
        if self.equal != computed:
            raise ValueError(
                "QualityComparison.equal must equal the conjunction of "
                "status_equal, exp_tasks_equal, and evidence_equal"
            )
        return self


class ExpectedMetrics(BaseModel):
    """HER-observed reference metrics declared in task data (plan item 6).

    Comparison-only numbers from ``runs_final_20261023``; never a quality gate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    dur_s: float = Field(ge=0.0)
    llm_calls: StrictInt = Field(ge=0)
    tokens: StrictInt = Field(ge=0)


class BenchTask(BaseModel):
    """One research scenario loaded from a versioned YAML task file."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    task_id: str = Field(min_length=1)
    task_set_version: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    inputs: tuple[str, ...] = ()
    driver: TaskDriver | None = None
    expected_metrics: ExpectedMetrics
    quality: QualityExpectation

    @model_validator(mode="after")
    def _relative_unique_input_paths(self) -> Self:
        for path in self.inputs:
            _require_relative_path(path, "BenchTask.inputs")
        if len(self.inputs) != len(set(self.inputs)):
            raise ValueError("BenchTask.inputs paths must be unique")
        return self


def _require_relative_path(path: str, field: str) -> None:
    """Refuse empty/whitespace, absolute, or ``..``-escaping declared paths."""
    if not path or path != path.strip() or any(ch.isspace() for ch in path):
        raise ValueError(f"{field} entries must be non-empty paths without whitespace")
    parsed = PurePosixPath(path)
    if parsed.is_absolute():
        raise ValueError(f"{field} entries must be relative paths (got absolute: {path!r})")
    if ".." in parsed.parts:
        raise ValueError(f"{field} entries must not contain '..' (got {path!r})")


def _require_relative_task_path(path: str) -> None:
    """Task-set-relative task file paths (manifest ``tasks`` entries)."""
    _require_relative_path(path, "TaskSetManifest.tasks")


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


class SuiteTaskRun(BaseModel):
    """One task's timed run inside a suite run, with its exact quality comparison.

    Fail-closed shape: a refused run carries no quality comparison (``None``),
    an ok run must carry one, and the comparison's task_id must match the run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    run: TimedRunResult
    quality: QualityComparison | None

    @model_validator(mode="after")
    def _quality_presence_matches_run_kind(self) -> Self:
        if isinstance(self.run, TimedRunOk) and self.quality is None:
            raise ValueError("SuiteTaskRun with an ok run must carry a QualityComparison")
        if isinstance(self.run, TimedRunRefused) and self.quality is not None:
            raise ValueError("SuiteTaskRun with a refused run must not carry a QualityComparison")
        if self.quality is not None and self.quality.task_id != self.run.task_id:
            raise ValueError(
                "SuiteTaskRun quality.task_id must match run.task_id "
                f"(quality={self.quality.task_id!r}, run={self.run.task_id!r})"
            )
        return self


class SuiteRunResult(BaseModel):
    """One runner's pass over a whole task set (per-task timed runs).

    Wall time and usage aggregates derive from the runs (see ``suite_wall_time_s``
    / ``suite_llm_calls`` / ``suite_tokens``) — never estimated, never asserted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    system_id: str = Field(min_length=1)
    task_set_id: str = Field(min_length=1)
    task_set_version: str = Field(min_length=1)
    runs: tuple[SuiteTaskRun, ...] = Field(min_length=1)


def llm_call_count(records: tuple[UsageRecorded, ...]) -> int:
    """LLM calls = number of journaled ``UsageRecorded`` payloads (not estimated)."""
    return len(records)


def token_total(records: tuple[UsageRecorded, ...]) -> int:
    """Sum of provider prompt+completion tokens across recorded calls."""
    return sum(r.usage.prompt_tokens + r.usage.completion_tokens for r in records)


def suite_usage_records(result: SuiteRunResult) -> tuple[UsageRecorded, ...]:
    """All ``UsageRecorded`` payloads of a suite run's ok runs, in run order."""
    records: list[UsageRecorded] = []
    for entry in result.runs:
        if isinstance(entry.run, TimedRunOk):
            records.extend(entry.run.usage_recorded)
    return tuple(records)


def suite_wall_time_s(result: SuiteRunResult) -> float:
    """Summed per-task wall time of a suite run (raw ``perf_counter`` deltas)."""
    return sum(entry.run.dur_s for entry in result.runs)


def suite_llm_calls(result: SuiteRunResult) -> int:
    """LLM-call count of a suite run = number of recorded ``UsageRecorded`` payloads."""
    return llm_call_count(suite_usage_records(result))


def suite_tokens(result: SuiteRunResult) -> int:
    """Provider token total of a suite run (prompt+completion, never estimated)."""
    return token_total(suite_usage_records(result))
