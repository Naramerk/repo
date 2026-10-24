"""Old vs new CoScientist benchmark harness.

Compares wall time, provider token usage, LLM/tool call counts, and quality
signals across a versioned task set. Systems integrate through ``BenchRunner``.
"""

from __future__ import annotations

from bench.models import (
    BenchTask,
    CallCounts,
    PlanCriterionExpectation,
    PlanCriterionObservation,
    ProviderTokenUsage,
    QualityExpectation,
    QualityObservation,
    QualityScore,
    RunnerRefuse,
    RunnerSuccess,
    TaskSetManifest,
    VerdictObservation,
)
from bench.protocol import BenchRunner

__all__ = [
    "BenchRunner",
    "BenchTask",
    "CallCounts",
    "PlanCriterionExpectation",
    "PlanCriterionObservation",
    "ProviderTokenUsage",
    "QualityExpectation",
    "QualityObservation",
    "QualityScore",
    "RunnerRefuse",
    "RunnerSuccess",
    "TaskSetManifest",
    "VerdictObservation",
]
