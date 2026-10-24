"""Unit tests for bench runner/suite models (plan item 4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bench.models import (
    EvidenceOutcomeCount,
    QualityObservation,
    RunnerRefuse,
    RunnerSuccess,
    TimedRunOk,
    TimedRunRefused,
    UsageRecorded,
    llm_call_count,
    token_total,
)
from coscientist.llm.types import Usage


def _usage(*, prompt: int = 10, completion: int = 5) -> Usage:
    return Usage(prompt_tokens=prompt, completion_tokens=completion, latency_ms=1.5)


def _record(*, role: str = "planner", prompt: int = 10, completion: int = 5) -> UsageRecorded:
    return UsageRecorded(
        study_id="study-1",
        task_id="task-1",
        attempt_id=None,
        role=role,
        usage=_usage(prompt=prompt, completion=completion),
    )


def _quality() -> QualityObservation:
    return QualityObservation(
        status="fulfilled",
        exp_tasks=2,
        evidence=(EvidenceOutcomeCount(outcome="success", count=2),),
        record_result=2,
    )


def test_usage_recorded_requires_provider_usage() -> None:
    record = _record()
    assert record.usage.prompt_tokens == 10
    assert record.task_id == "task-1"
    assert record.attempt_id is None


def test_usage_recorded_rejects_empty_role() -> None:
    with pytest.raises(ValidationError):
        UsageRecorded(
            study_id="study-1",
            task_id=None,
            attempt_id=None,
            role="",
            usage=_usage(),
        )


def test_usage_recorded_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        UsageRecorded(
            study_id="study-1",
            task_id=None,
            attempt_id=None,
            role="planner",
            usage=_usage(),
            estimated_tokens=99,  # type: ignore[call-arg]
        )


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
    assert llm_call_count(()) == 0


def test_runner_success_carries_usage_recorded_not_call_counts() -> None:
    ok = RunnerSuccess(
        kind="success",
        usage_recorded=(_record(), _record(role="executor")),
        quality=_quality(),
    )
    assert ok.kind == "success"
    assert llm_call_count(ok.usage_recorded) == 2
    assert not hasattr(ok, "calls")


def test_runner_refuse_requires_reason() -> None:
    refused = RunnerRefuse(kind="refused", reason_code="no_orchestrator", detail="missing")
    assert refused.reason_code == "no_orchestrator"
    with pytest.raises(ValidationError):
        RunnerRefuse(kind="refused", reason_code="", detail="x")


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
    run = TimedRunRefused(
        kind="refused",
        system_id="old",
        task_id="web",
        dur_s=0.5,
        reason_code="branch_mismatch",
        detail="expected dev_1",
    )
    with pytest.raises(ValidationError):
        run.detail = "other"  # type: ignore[misc]


def test_quality_observation_rejects_duplicate_evidence() -> None:
    with pytest.raises(ValidationError, match="unique"):
        QualityObservation(
            status="partial",
            exp_tasks=1,
            evidence=(
                EvidenceOutcomeCount(outcome="success", count=1),
                EvidenceOutcomeCount(outcome="success", count=1),
            ),
            record_result=0,
        )
