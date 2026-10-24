"""Unit tests for bench runner/suite models (plan item 4)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from bench.models import (
    QualityObservation,
    RunnerRefuse,
    RunnerSuccess,
    TaskComparison,
    TimedRunOk,
    TimedRunRefused,
    UsageRecorded,
    llm_call_count,
    token_total,
)


def _usage_payload(*, prompt: int = 10, completion: int = 5) -> dict[str, Any]:
    return {"prompt_tokens": prompt, "completion_tokens": completion, "latency_ms": 1.5}


def _record_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "study_id": "study-1",
        "task_id": "task-1",
        "attempt_id": None,
        "role": "planner",
        "usage": _usage_payload(),
    }
    payload.update(overrides)
    return payload


def _record(*, role: str = "planner", prompt: int = 10, completion: int = 5) -> UsageRecorded:
    return UsageRecorded.model_validate(
        _record_payload(role=role, usage=_usage_payload(prompt=prompt, completion=completion))
    )


def _quality_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "fulfilled",
        "exp_tasks": 2,
        "evidence": ({"outcome": "success", "count": 2},),
        "record_result": 2,
    }
    payload.update(overrides)
    return payload


def _quality() -> QualityObservation:
    return QualityObservation.model_validate(_quality_payload())


def test_usage_recorded_requires_provider_usage() -> None:
    record = _record()
    assert record.usage.prompt_tokens == 10
    assert record.task_id == "task-1"
    assert record.attempt_id is None


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (_record_payload(role=""), r"String should have at least 1 character"),
        (
            _record_payload(estimated_tokens=99),
            r"Extra inputs are not permitted",
        ),
    ],
    ids=["empty_role", "extra_field"],
)
def test_usage_recorded_rejects(payload: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        UsageRecorded.model_validate(payload)


def test_llm_call_count_is_len_of_usage_recorded() -> None:
    records = (_record(role="a"), _record(role="b"), _record(role="c"))
    assert llm_call_count(records) == 3


def test_token_total_sums_provider_prompt_and_completion() -> None:
    records = (
        _record(prompt=100, completion=20),
        _record(prompt=3, completion=7),
    )
    assert token_total(records) == 130


def test_token_total_empty_is_zero() -> None:
    assert token_total(()) == 0


def test_llm_call_count_empty_is_zero() -> None:
    assert llm_call_count(()) == 0


def test_runner_success_carries_usage_recorded() -> None:
    ok = RunnerSuccess(
        kind="success",
        usage_recorded=(_record(), _record(role="executor")),
        quality=_quality(),
    )
    assert ok.kind == "success"
    assert llm_call_count(ok.usage_recorded) == 2


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {"kind": "refused", "reason_code": "", "detail": "x"},
            r"String should have at least 1 character",
        ),
    ],
    ids=["empty_reason_code"],
)
def test_runner_refuse_rejects(payload: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        RunnerRefuse.model_validate(payload)


def test_runner_refuse_accepts_reason() -> None:
    refused = RunnerRefuse.model_validate({"kind": "refused", "reason_code": "no_orchestrator", "detail": "missing"})
    assert refused.reason_code == "no_orchestrator"


def test_timed_run_ok_her_fields() -> None:
    run = TimedRunOk(
        kind="ok",
        system_id="new",
        task_id="two_tasks",
        dur_s=1172.0,
        usage_recorded=(_record(prompt=50, completion=3),),
        quality=_quality(),
    )
    assert run.dur_s == 1172.0
    assert token_total(run.usage_recorded) == 53
    assert run.quality.record_result == 2


def test_timed_run_refused_frozen() -> None:
    run = TimedRunRefused.model_validate(
        {
            "kind": "refused",
            "system_id": "old",
            "task_id": "web",
            "dur_s": 0.5,
            "reason_code": "branch_mismatch",
            "detail": "expected dev_1",
        }
    )
    field_name = "detail"
    with pytest.raises(ValidationError, match=r"Instance is frozen"):
        setattr(run, field_name, "other")


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            _quality_payload(
                status="partial",
                exp_tasks=1,
                evidence=(
                    {"outcome": "success", "count": 1},
                    {"outcome": "success", "count": 1},
                ),
                record_result=0,
            ),
            r"QualityObservation\.evidence outcomes must be unique",
        ),
    ],
    ids=["duplicate_evidence"],
)
def test_quality_observation_rejects(payload: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        QualityObservation.model_validate(payload)


def test_task_comparison_requires_matching_task_ids() -> None:
    old = TimedRunRefused.model_validate(
        {
            "kind": "refused",
            "system_id": "old",
            "task_id": "two_tasks",
            "dur_s": 1.0,
            "reason_code": "r",
            "detail": "d",
        }
    )
    new = TimedRunOk(
        kind="ok",
        system_id="new",
        task_id="two_tasks",
        dur_s=2.0,
        usage_recorded=(),
        quality=_quality(),
    )
    cmp_ = TaskComparison(task_id="two_tasks", old=old, new=new)
    assert cmp_.task_id == "two_tasks"


@pytest.mark.parametrize(
    ("old_task_id", "new_task_id"),
    [
        ("other", "two_tasks"),
        ("two_tasks", "other"),
    ],
    ids=["mismatched_old", "mismatched_new"],
)
def test_task_comparison_rejects_mismatched_task_ids(old_task_id: str, new_task_id: str) -> None:
    old = TimedRunRefused.model_validate(
        {
            "kind": "refused",
            "system_id": "old",
            "task_id": old_task_id,
            "dur_s": 1.0,
            "reason_code": "r",
            "detail": "d",
        }
    )
    new = TimedRunOk(
        kind="ok",
        system_id="new",
        task_id=new_task_id,
        dur_s=2.0,
        usage_recorded=(),
        quality=_quality(),
    )
    with pytest.raises(
        ValidationError,
        match=r"TaskComparison\.task_id must equal old\.task_id and new\.task_id",
    ):
        TaskComparison(task_id="two_tasks", old=old, new=new)
