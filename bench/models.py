"""Task set and quality expectation models (benchmark harness plan item 3).

Fields are required only — no optional defaults, no inferred quality.
Quality keys match plan item 8 / HER extract: status, exp_tasks, evidence.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


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


class TaskSetManifest(BaseModel):
    """Index of relative task YAML paths for a versioned set."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    task_set_id: str = Field(min_length=1)
    task_set_version: str = Field(min_length=1)
    tasks: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _nonempty_task_paths(self) -> TaskSetManifest:
        for path in self.tasks:
            if not path or path != path.strip() or any(ch.isspace() for ch in path):
                raise ValueError(
                    "TaskSetManifest.tasks entries must be non-empty paths "
                    "without whitespace"
                )
        if len(self.tasks) != len(set(self.tasks)):
            raise ValueError("TaskSetManifest.tasks paths must be unique")
        return self
