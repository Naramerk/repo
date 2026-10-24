"""Tests for per-run usage.jsonl and result.json writers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.errors import BenchConfigError
from bench.models import (
    EvidenceOutcomeCount,
    QualityObservation,
    TimedRunOk,
    TimedRunRefused,
    UsageRecorded,
)
from bench.write_artifacts import (
    timed_run_to_result_obj,
    write_result_json,
    write_usage_jsonl,
)
from coscientist.llm.types import Usage


def _records() -> tuple[UsageRecorded, ...]:
    return (
        UsageRecorded(
            study_id="s1",
            task_id="two_tasks",
            attempt_id="a1",
            role="planner",
            usage=Usage(prompt_tokens=100, completion_tokens=20, latency_ms=12.0),
        ),
        UsageRecorded(
            study_id="s1",
            task_id="two_tasks",
            attempt_id=None,
            role="executor",
            usage=Usage(prompt_tokens=5, completion_tokens=5, latency_ms=3.0),
        ),
    )


def test_write_usage_jsonl_one_object_per_call(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    write_usage_jsonl(path, _records())
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["role"] == "planner"
    assert first["prompt_tokens"] == 100
    assert first["completion_tokens"] == 20
    assert first["latency_ms"] == 12.0
    assert first["task_id"] == "two_tasks"
    assert first["attempt_id"] == "a1"
    second = json.loads(lines[1])
    assert second["attempt_id"] is None
    assert "estimated" not in first


def test_write_usage_jsonl_empty_writes_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    write_usage_jsonl(path, ())
    assert path.read_text(encoding="utf-8") == ""


def test_write_usage_jsonl_refuses_missing_parent(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "usage.jsonl"
    with pytest.raises(BenchConfigError, match="parent directory missing"):
        write_usage_jsonl(path, _records())


def test_write_usage_jsonl_refuses_parent_that_is_not_directory(tmp_path: Path) -> None:
    parent_as_file = tmp_path / "not_a_dir"
    parent_as_file.write_text("x", encoding="utf-8")
    path = parent_as_file / "usage.jsonl"
    with pytest.raises(BenchConfigError, match="parent is not a directory"):
        write_usage_jsonl(path, _records())


def test_timed_run_to_result_obj_assert_never_on_unknown_type() -> None:
    with pytest.raises(AssertionError):
        timed_run_to_result_obj(object())  # type: ignore[arg-type]


def test_write_result_json_ok_her_shape(tmp_path: Path) -> None:
    run = TimedRunOk(
        kind="ok",
        system_id="new",
        task_id="two_tasks",
        dur_s=1172.0,
        usage_recorded=_records(),
        quality=QualityObservation(
            status="fulfilled",
            exp_tasks=2,
            evidence=(EvidenceOutcomeCount(outcome="success", count=2),),
            record_result=2,
        ),
    )
    path = tmp_path / "result.json"
    write_result_json(path, run)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "dur_s": 1172.0,
        "evidence": [{"count": 2, "outcome": "success"}],
        "exp_tasks": 2,
        "kind": "ok",
        "llm_calls": 2,
        "record_result": 2,
        "status": "fulfilled",
        "system_id": "new",
        "task_id": "two_tasks",
        "tokens": 130,
    }


def test_write_result_json_refused(tmp_path: Path) -> None:
    run = TimedRunRefused(
        kind="refused",
        system_id="old",
        task_id="web",
        dur_s=0.25,
        reason_code="branch_mismatch",
        detail="expected dev_1",
    )
    path = tmp_path / "result.json"
    write_result_json(path, run)
    payload = timed_run_to_result_obj(run)
    assert payload["kind"] == "refused"
    assert payload["reason_code"] == "branch_mismatch"
    assert "tokens" not in payload
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_write_result_json_refuses_directory_path(tmp_path: Path) -> None:
    with pytest.raises(BenchConfigError, match="directory"):
        write_result_json(
            tmp_path,
            TimedRunRefused(
                kind="refused",
                system_id="x",
                task_id="y",
                dur_s=0.0,
                reason_code="r",
                detail="d",
            ),
        )
