"""Run the same task set on two BenchRunner implementations and compare."""

from __future__ import annotations

import time
from pathlib import Path

from bench.errors import BenchConfigError
from bench.load_tasks import load_task_set
from bench.models import (
    BenchSuiteResult,
    BenchTask,
    RunnerRefuse,
    RunnerSuccess,
    TaskComparison,
    TimedRunOk,
    TimedRunRefused,
    TimedRunResult,
)
from bench.protocol import BenchRunner
from bench.quality import score_quality
from bench.report import write_json_result, write_markdown_summary


async def _time_run(runner: BenchRunner, task: BenchTask) -> TimedRunResult:
    t0 = time.perf_counter()
    raw = await runner.run_task(task)
    wall_ms = (time.perf_counter() - t0) * 1000.0
    if isinstance(raw, RunnerRefuse):
        return TimedRunRefused(
            kind="refused",
            system_id=runner.system_id,
            task_id=task.task_id,
            wall_time_ms=wall_ms,
            reason_code=raw.reason_code,
            detail=raw.detail,
        )
    if not isinstance(raw, RunnerSuccess):
        raise BenchConfigError(
            f"runner {runner.system_id!r} returned unsupported result type "
            f"{type(raw).__name__}"
        )
    return TimedRunOk(
        kind="ok",
        system_id=runner.system_id,
        task_id=task.task_id,
        wall_time_ms=wall_ms,
        tokens=raw.tokens,
        calls=raw.calls,
        quality=raw.quality,
        quality_score=score_quality(task.quality, raw.quality),
    )


async def run_suite(
    *,
    manifest_path: Path,
    old_runner: BenchRunner,
    new_runner: BenchRunner,
) -> BenchSuiteResult:
    if old_runner.system_id == new_runner.system_id:
        raise BenchConfigError(
            "old_runner and new_runner must have distinct system_id values "
            f"(both {old_runner.system_id!r})"
        )
    manifest, tasks = load_task_set(manifest_path)
    comparisons: list[TaskComparison] = []
    for task in tasks:
        old_result = await _time_run(old_runner, task)
        new_result = await _time_run(new_runner, task)
        comparisons.append(
            TaskComparison(task_id=task.task_id, old=old_result, new=new_result)
        )
    return BenchSuiteResult(
        task_set_id=manifest.task_set_id,
        task_set_version=manifest.task_set_version,
        comparisons=tuple(comparisons),
    )


async def run_suite_and_write(
    *,
    manifest_path: Path,
    old_runner: BenchRunner,
    new_runner: BenchRunner,
    out_dir: Path,
) -> BenchSuiteResult:
    """Run the suite and write ``result.json`` + ``summary.md`` under ``out_dir``."""
    suite = await run_suite(
        manifest_path=manifest_path,
        old_runner=old_runner,
        new_runner=new_runner,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json_result(out_dir / "result.json", suite)
    write_markdown_summary(out_dir / "summary.md", suite)
    return suite
