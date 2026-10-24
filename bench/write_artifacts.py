"""Per-run ``usage.jsonl`` and ``result.json`` writers (HER / make_summary shape).

Tokens and LLM-call counts come only from ``UsageRecorded`` + provider ``Usage``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, assert_never

from bench.errors import BenchConfigError
from bench.models import (
    TimedRunOk,
    TimedRunRefused,
    TimedRunResult,
    UsageRecorded,
    llm_call_count,
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
