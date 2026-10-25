"""Suite harness (plan item 11): run tasks, wall-time, compare, write artifacts.

Runs one :class:`BenchRunner` over loaded task data, wall-times every task with
``time.perf_counter``, attaches the exact quality comparison and the runner's
``UsageRecorded`` payloads, and writes the suite artifacts via
``bench.write_artifacts`` (``result.json`` / ``summary.md`` / ``usage.jsonl``).
Tokens and call counts come only from runner-reported provider usage.
"""

from __future__ import annotations

import time
from pathlib import Path

from bench.errors import BenchConfigError
from bench.models import (
    BenchTask,
    RunnerSuccess,
    SuiteRunResult,
    SuiteTaskRun,
    TimedRunOk,
    TimedRunRefused,
    TimedRunResult,
    suite_usage_records,
)
from bench.quality import compare_quality
from bench.runner import BenchRunner
from bench.write_artifacts import (
    write_suite_result_json,
    write_suite_summary_md,
    write_usage_jsonl,
)


def _require_runner(runner: BenchRunner) -> None:
    if not isinstance(runner, BenchRunner):
        raise BenchConfigError(
            f"runner {runner!r} does not satisfy the BenchRunner protocol (missing run(task))"
        )


def time_run(runner: BenchRunner, task: BenchTask, system_id: str) -> TimedRunResult:
    """Wall-time one ``runner.run`` call and wrap its typed result."""
    _require_runner(runner)
    start = time.perf_counter()
    result = runner.run(task)
    dur_s = time.perf_counter() - start
    if isinstance(result, RunnerSuccess):
        return TimedRunOk(
            kind="ok",
            system_id=system_id,
            task_id=task.task_id,
            dur_s=dur_s,
            usage_recorded=result.usage_recorded,
            quality=result.quality,
        )
    return TimedRunRefused(
        kind="refused",
        system_id=system_id,
        task_id=task.task_id,
        dur_s=dur_s,
        reason_code=result.reason_code,
        detail=result.detail,
    )


def run_suite(
    runner: BenchRunner,
    tasks: tuple[BenchTask, ...],
    *,
    system_id: str,
    task_set_id: str,
    task_set_version: str,
) -> SuiteRunResult:
    """Run every task through the runner; attach timing, usage, quality compare."""
    _require_runner(runner)
    entries: list[SuiteTaskRun] = []
    for task in tasks:
        timed = time_run(runner, task, system_id)
        quality = compare_quality(task, timed.quality) if isinstance(timed, TimedRunOk) else None
        entries.append(SuiteTaskRun(run=timed, quality=quality))
    return SuiteRunResult(
        system_id=system_id,
        task_set_id=task_set_id,
        task_set_version=task_set_version,
        runs=tuple(entries),
    )


def write_suite_artifacts(out_dir: Path, result: SuiteRunResult) -> None:
    """Write ``result.json`` + ``summary.md`` + ``usage.jsonl`` into ``out_dir``.

    ``out_dir`` is created if absent; an existing non-directory path is refused.
    """
    if out_dir.exists() and not out_dir.is_dir():
        raise BenchConfigError(f"refusing to write: output path is not a directory: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    write_suite_result_json(out_dir / "result.json", result)
    write_suite_summary_md(out_dir / "summary.md", result)
    write_usage_jsonl(out_dir / "usage.jsonl", suite_usage_records(result))
