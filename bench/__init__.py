"""Benchmark harness package (old vs new CoScientist)."""

from __future__ import annotations

from bench.errors import BenchConfigError, BenchError
from bench.models import (
    BenchSuiteResult,
    BenchTask,
    EvidenceOutcomeCount,
    ExpectedMetrics,
    QualityExpectation,
    QualityObservation,
    RunnerRefuse,
    RunnerSuccess,
    TaskComparison,
    TaskSetManifest,
    TimedRunOk,
    TimedRunRefused,
    UsageRecorded,
    llm_call_count,
    token_total,
)
from bench.write_artifacts import write_result_json, write_usage_jsonl

__all__ = [
    "BenchConfigError",
    "BenchError",
    "BenchSuiteResult",
    "BenchTask",
    "EvidenceOutcomeCount",
    "ExpectedMetrics",
    "QualityExpectation",
    "QualityObservation",
    "RunnerRefuse",
    "RunnerSuccess",
    "TaskComparison",
    "TaskSetManifest",
    "TimedRunOk",
    "TimedRunRefused",
    "UsageRecorded",
    "llm_call_count",
    "token_total",
    "write_result_json",
    "write_usage_jsonl",
]
