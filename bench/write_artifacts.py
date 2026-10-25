"""Per-run and per-suite artifact writers (HER / make_summary shape).

Tokens and LLM-call counts come only from ``UsageRecorded`` + provider ``Usage``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, assert_never

from bench.errors import BenchConfigError
from bench.models import (
    QualityComparison,
    SuiteRunResult,
    SuiteTaskRun,
    TimedRunOk,
    TimedRunRefused,
    TimedRunResult,
    UsageRecorded,
    llm_call_count,
    suite_llm_calls,
    suite_tokens,
    suite_wall_time_s,
    token_total,
)


def _require_writable_file_path(path: Path) -> None:
    if path.exists() and path.is_dir():
        raise BenchConfigError(f"refusing to write: path is a directory: {path}")
    parent = path.parent
    if not parent.exists():
        raise BenchConfigError(f"refusing to write: parent directory missing: {parent}")
    if not parent.is_dir():
        raise BenchConfigError(f"refusing to write: parent is not a directory: {parent}")


def usage_recorded_to_jsonl_obj(record: UsageRecorded) -> dict[str, Any]:
    """One usage.jsonl object: UsageRecorded fields + flat provider token counts."""
    return {
        "study_id": record.study_id,
        "task_id": record.task_id,
        "attempt_id": record.attempt_id,
        "role": record.role,
        "prompt_tokens": record.usage.prompt_tokens,
        "completion_tokens": record.usage.completion_tokens,
        "latency_ms": record.usage.latency_ms,
    }


def write_usage_jsonl(path: Path, records: tuple[UsageRecorded, ...]) -> None:
    """Write one JSON object per LLM call (provider tokens only)."""
    _require_writable_file_path(path)
    lines = [
        json.dumps(usage_recorded_to_jsonl_obj(record), separators=(",", ":"), sort_keys=True) for record in records
    ]
    text = "\n".join(lines)
    if lines:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def timed_run_to_result_obj(run: TimedRunResult) -> dict[str, Any]:
    """HER-comparable result.json object (status, dur_s, llm_calls, tokens, …)."""
    if isinstance(run, TimedRunRefused):
        return {
            "kind": "refused",
            "system_id": run.system_id,
            "task_id": run.task_id,
            "dur_s": run.dur_s,
            "reason_code": run.reason_code,
            "detail": run.detail,
        }
    if isinstance(run, TimedRunOk):
        return {
            "kind": "ok",
            "system_id": run.system_id,
            "task_id": run.task_id,
            "status": run.quality.status,
            "dur_s": run.dur_s,
            "llm_calls": llm_call_count(run.usage_recorded),
            "tokens": token_total(run.usage_recorded),
            "exp_tasks": run.quality.exp_tasks,
            "evidence": [{"outcome": row.outcome, "count": row.count} for row in run.quality.evidence],
            "record_result": run.quality.record_result,
        }
    assert_never(run)


def write_result_json(path: Path, run: TimedRunResult) -> None:
    """Write per-run result.json (make_summary-comparable fields)."""
    _require_writable_file_path(path)
    payload = timed_run_to_result_obj(run)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _quality_to_obj(quality: QualityComparison) -> dict[str, Any]:
    return {
        "task_id": quality.task_id,
        "status_equal": quality.status_equal,
        "exp_tasks_equal": quality.exp_tasks_equal,
        "evidence_equal": quality.evidence_equal,
        "equal": quality.equal,
    }


def suite_task_run_to_obj(entry: SuiteTaskRun) -> dict[str, Any]:
    """One suite-run task entry: the per-run object plus its quality comparison."""
    payload = timed_run_to_result_obj(entry.run)
    payload["quality"] = _quality_to_obj(entry.quality) if entry.quality is not None else None
    return payload


def suite_run_to_result_obj(result: SuiteRunResult) -> dict[str, Any]:
    """Suite result.json object: wall time + Usage aggregates + per-task runs."""
    return {
        "system_id": result.system_id,
        "task_set_id": result.task_set_id,
        "task_set_version": result.task_set_version,
        "wall_time_s": suite_wall_time_s(result),
        "llm_calls": suite_llm_calls(result),
        "tokens": suite_tokens(result),
        "runs": [suite_task_run_to_obj(entry) for entry in result.runs],
    }


def write_suite_result_json(path: Path, result: SuiteRunResult) -> None:
    """Write the suite run result.json (raw wall time, provider usage aggregates)."""
    _require_writable_file_path(path)
    payload = suite_run_to_result_obj(result)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _format_seconds(value: float) -> str:
    return f"{value:.3f}"


def suite_run_to_summary_md(result: SuiteRunResult) -> str:
    """Suite summary.md: totals line + one row per task (make_summary shape)."""
    lines = [
        f"# Bench suite run — system {result.system_id}",
        "",
        f"Task set: {result.task_set_id} v{result.task_set_version}",
        "",
        "Totals: "
        f"wall_time_s={_format_seconds(suite_wall_time_s(result))} "
        f"llm_calls={suite_llm_calls(result)} "
        f"tokens={suite_tokens(result)}",
        "",
        "| task | kind | status | dur_s | llm_calls | tokens | quality |",
        "|---|---|---|---|---|---|---|",
    ]
    for entry in result.runs:
        run = entry.run
        if isinstance(run, TimedRunOk):
            lines.append(
                "| {task} | ok | {status} | {dur} | {calls} | {tokens} | {quality} |".format(
                    task=run.task_id,
                    status=run.quality.status,
                    dur=_format_seconds(run.dur_s),
                    calls=llm_call_count(run.usage_recorded),
                    tokens=token_total(run.usage_recorded),
                    quality="match" if entry.quality is not None and entry.quality.equal else "mismatch",
                )
            )
        else:
            lines.append(
                "| {task} | refused | {reason} | {dur} | - | - | - |".format(
                    task=run.task_id,
                    reason=run.reason_code,
                    dur=_format_seconds(run.dur_s),
                )
            )
    lines.append("")
    return "\n".join(lines)


def write_suite_summary_md(path: Path, result: SuiteRunResult) -> None:
    """Write the suite run summary.md."""
    _require_writable_file_path(path)
    path.write_text(suite_run_to_summary_md(result), encoding="utf-8")
