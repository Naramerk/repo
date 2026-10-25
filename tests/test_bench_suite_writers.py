"""Unit tests for suite-run models and report writers (plan items 4/9)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from bench.errors import BenchConfigError
from bench.models import (
    QualityComparison,
    QualityObservation,
    RunnerSuccess,
    SuiteRunResult,
    SuiteTaskRun,
    TimedRunOk,
    TimedRunRefused,
    UsageRecorded,
    suite_llm_calls,
    suite_tokens,
    suite_usage_records,
    suite_wall_time_s,
)
from bench.write_artifacts import (
    suite_run_to_result_obj,
    suite_run_to_summary_md,
    write_suite_result_json,
    write_suite_summary_md,
)


def _record(*, prompt: int = 10, completion: int = 5) -> UsageRecorded:
    return UsageRecorded.model_validate(
        {
            "study_id": "study-1",
            "task_id": "two_tasks",
            "attempt_id": None,
            "role": "planner",
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "latency_ms": 1.5,
            },
        }
    )


def _quality_observation(**overrides: Any) -> QualityObservation:
    payload: dict[str, Any] = {
        "status": "fulfilled",
        "exp_tasks": 2,
        "evidence": ({"outcome": "success", "count": 2},),
        "record_result": 2,
    }
    payload.update(overrides)
    return QualityObservation.model_validate(payload)


def _comparison(task_id: str, *, equal: bool = True) -> QualityComparison:
    return QualityComparison.model_validate(
        {
            "task_id": task_id,
            "status_equal": equal,
            "exp_tasks_equal": True,
            "evidence_equal": True,
            "equal": equal,
        }
    )


def _ok_run(task_id: str, *, dur_s: float, records: tuple[UsageRecorded, ...]) -> TimedRunOk:
    return TimedRunOk(
        kind="ok",
        system_id="fake",
        task_id=task_id,
        dur_s=dur_s,
        usage_recorded=records,
        quality=_quality_observation(),
    )


def _refused_run(task_id: str) -> TimedRunRefused:
    return TimedRunRefused.model_validate(
        {
            "kind": "refused",
            "system_id": "fake",
            "task_id": task_id,
            "dur_s": 0.25,
            "reason_code": "driver_unimplemented",
            "detail": "driver 'web' not implemented",
        }
    )


def _suite_result() -> SuiteRunResult:
    return SuiteRunResult(
        system_id="fake",
        task_set_id="her_runs_final_20261023",
        task_set_version="1",
        runs=(
            SuiteTaskRun(
                run=_ok_run("two_tasks", dur_s=2.0, records=(_record(prompt=100, completion=23),)),
                quality=_comparison("two_tasks"),
            ),
            SuiteTaskRun(run=_refused_run("web"), quality=None),
            SuiteTaskRun(
                run=_ok_run("borshevik", dur_s=3.5, records=(_record(), _record(prompt=7, completion=3))),
                quality=_comparison("borshevik", equal=False),
            ),
        ),
    )


def test_suite_task_run_ok_requires_quality() -> None:
    with pytest.raises(ValidationError, match=r"ok run must carry a QualityComparison"):
        SuiteTaskRun(run=_ok_run("two_tasks", dur_s=1.0, records=()), quality=None)


def test_suite_task_run_refused_forbids_quality() -> None:
    with pytest.raises(ValidationError, match=r"refused run must not carry a QualityComparison"):
        SuiteTaskRun(run=_refused_run("web"), quality=_comparison("web"))


def test_suite_task_run_task_ids_must_match() -> None:
    with pytest.raises(ValidationError, match=r"quality\.task_id must match run\.task_id"):
        SuiteTaskRun(run=_ok_run("two_tasks", dur_s=1.0, records=()), quality=_comparison("other"))


def test_suite_aggregates_sums_across_ok_runs() -> None:
    result = _suite_result()
    assert suite_wall_time_s(result) == pytest.approx(5.75)
    assert suite_llm_calls(result) == 3
    assert suite_tokens(result) == (100 + 23) + (10 + 5) + (7 + 3)
    assert len(suite_usage_records(result)) == 3


def test_suite_result_json_object_shape(tmp_path: Path) -> None:
    result = _suite_result()
    payload = suite_run_to_result_obj(result)
    assert payload["system_id"] == "fake"
    assert payload["task_set_id"] == "her_runs_final_20261023"
    assert payload["task_set_version"] == "1"
    assert payload["wall_time_s"] == pytest.approx(5.75)
    assert payload["llm_calls"] == 3
    assert payload["tokens"] == 148
    assert [r["task_id"] for r in payload["runs"]] == ["two_tasks", "web", "borshevik"]
    assert payload["runs"][0]["quality"]["equal"] is True
    assert payload["runs"][1]["kind"] == "refused"
    assert payload["runs"][1]["quality"] is None
    assert payload["runs"][2]["quality"]["equal"] is False

    write_suite_result_json(tmp_path / "result.json", result)
    written = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert written == payload


def test_suite_summary_md_shape(tmp_path: Path) -> None:
    result = _suite_result()
    md = suite_run_to_summary_md(result)
    assert "# Bench suite run — system fake" in md
    assert "Task set: her_runs_final_20261023 v1" in md
    assert "wall_time_s=5.750 llm_calls=3 tokens=148" in md
    assert "| two_tasks | ok | fulfilled | 2.000 | 1 | 123 | match |" in md
    assert "| web | refused | driver_unimplemented | 0.250 | - | - | - |" in md
    assert "| borshevik | ok | fulfilled | 3.500 | 2 | 25 | mismatch |" in md

    write_suite_summary_md(tmp_path / "summary.md", result)
    assert (tmp_path / "summary.md").read_text(encoding="utf-8") == md


def test_suite_writers_refuse_missing_parent(tmp_path: Path) -> None:
    result = _suite_result()
    with pytest.raises(BenchConfigError, match="parent directory missing"):
        write_suite_result_json(tmp_path / "absent" / "result.json", result)
    with pytest.raises(BenchConfigError, match="parent directory missing"):
        write_suite_summary_md(tmp_path / "absent" / "summary.md", result)


def test_runner_success_roundtrip_still_valid() -> None:
    ok = RunnerSuccess(
        kind="success",
        usage_recorded=(_record(),),
        quality=_quality_observation(),
    )
    assert ok.kind == "success"
