"""Benchmark harness package (old vs new CoScientist)."""

from __future__ import annotations

from bench.errors import BenchConfigError, BenchError
from bench.fake import FakeRunner, RefusingFakeRunner, fake_refusal, fake_success
from bench.models import (
    BenchSuiteResult,
    BenchTask,
    EvidenceOutcomeCount,
    ExpectedMetrics,
    QualityComparison,
    QualityExpectation,
    QualityObservation,
    RunnerRefuse,
    RunnerSuccess,
    SuiteRunResult,
    SuiteTaskRun,
    TaskComparison,
    TaskSetManifest,
    TimedRunOk,
    TimedRunRefused,
    UsageRecorded,
    llm_call_count,
    suite_llm_calls,
    suite_tokens,
    suite_usage_records,
    suite_wall_time_s,
    token_total,
)
from bench.quality import compare_quality
from bench.runner import BenchRunner
from bench.suite import run_suite, time_run, write_suite_artifacts
from bench.write_artifacts import (
    suite_run_to_result_obj,
    suite_run_to_summary_md,
    write_result_json,
    write_suite_result_json,
    write_suite_summary_md,
    write_usage_jsonl,
)

__all__ = [
    "BenchConfigError",
    "BenchError",
    "BenchRunner",
    "BenchSuiteResult",
    "BenchTask",
    "EvidenceOutcomeCount",
    "ExpectedMetrics",
    "FakeRunner",
    "QualityComparison",
    "QualityExpectation",
    "QualityObservation",
    "RefusingFakeRunner",
    "RunnerRefuse",
    "RunnerSuccess",
    "SuiteRunResult",
    "SuiteTaskRun",
    "TaskComparison",
    "TaskSetManifest",
    "TimedRunOk",
    "TimedRunRefused",
    "UsageRecorded",
    "compare_quality",
    "fake_refusal",
    "fake_success",
    "llm_call_count",
    "run_suite",
    "suite_llm_calls",
    "suite_tokens",
    "suite_usage_records",
    "suite_wall_time_s",
    "token_total",
    "suite_run_to_result_obj",
    "suite_run_to_summary_md",
    "time_run",
    "write_result_json",
    "write_suite_result_json",
    "write_suite_summary_md",
    "write_suite_artifacts",
    "write_usage_jsonl",
]
