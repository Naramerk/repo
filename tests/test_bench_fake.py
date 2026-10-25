"""Unit tests for bench.fake deterministic runners (plan item 10)."""

from __future__ import annotations

from typing import Any

import pytest

from bench.errors import BenchConfigError
from bench.fake import (
    FakeRunner,
    RefusingFakeRunner,
    fake_quality_observation,
    fake_refusal,
    fake_success,
    fake_usage_recorded,
)
from bench.models import BenchTask, llm_call_count, token_total


def _task(**overrides: Any) -> BenchTask:
    payload: dict[str, Any] = {
        "task_id": "two_tasks",
        "task_set_version": "1",
        "prompt": "prompt",
        "expected_metrics": {"dur_s": 1172.0, "llm_calls": 53, "tokens": 1097723},
        "quality": {
            "status": "fulfilled",
            "exp_tasks": 2,
            "evidence": ({"outcome": "success", "count": 2},),
        },
    }
    payload.update(overrides)
    return BenchTask.model_validate(payload)


def test_fake_usage_recorded_replays_metrics_exactly() -> None:
    records = fake_usage_recorded(_task())
    assert llm_call_count(records) == 53
    assert token_total(records) == 1097723


def test_fake_usage_recorded_split_rule() -> None:
    # 13 tokens over 5 calls: base=2, remainder=3 -> first 3 records get 3, rest 2.
    records = fake_usage_recorded(_task(expected_metrics={"dur_s": 1.0, "llm_calls": 5, "tokens": 13}))
    assert [r.usage.prompt_tokens for r in records] == [3, 3, 3, 2, 2]
    assert all(r.usage.completion_tokens == 0 for r in records)
    assert all(r.usage.latency_ms == 0.0 for r in records)
    assert all(r.role == "fake" for r in records)
    assert all(r.study_id == "two_tasks" for r in records)
    assert all(r.task_id == "two_tasks" for r in records)
    assert all(r.attempt_id is None for r in records)


def test_fake_usage_recorded_single_call() -> None:
    records = fake_usage_recorded(_task(expected_metrics={"dur_s": 1.0, "llm_calls": 1, "tokens": 10}))
    assert len(records) == 1
    assert records[0].usage.prompt_tokens == 10


def test_fake_usage_recorded_zero_calls_zero_tokens() -> None:
    assert fake_usage_recorded(_task(expected_metrics={"dur_s": 1.0, "llm_calls": 0, "tokens": 0})) == ()


def test_fake_usage_recorded_refuses_contradictory_metrics() -> None:
    with pytest.raises(BenchConfigError, match="llm_calls=0 but tokens=5"):
        fake_usage_recorded(_task(expected_metrics={"dur_s": 1.0, "llm_calls": 0, "tokens": 5}))


def test_fake_quality_observation_replays_expected() -> None:
    task = _task(
        task_id="borshevik",
        quality={
            "status": "partial",
            "exp_tasks": 1,
            "evidence": ({"outcome": "null", "count": 1}, {"outcome": "success", "count": 1}),
        },
    )
    observed = fake_quality_observation(task)
    assert observed.status == "partial"
    assert observed.exp_tasks == 1
    assert observed.evidence == task.quality.evidence
    assert observed.record_result == 2


def test_fake_success_shape() -> None:
    result = fake_success(_task())
    assert result.kind == "success"
    assert llm_call_count(result.usage_recorded) == 53
    assert token_total(result.usage_recorded) == 1097723
    assert result.quality.status == "fulfilled"


def test_fake_runner_is_deterministic_and_task_driven() -> None:
    runner = FakeRunner()
    task_a = _task(task_id="a", expected_metrics={"dur_s": 1.0, "llm_calls": 2, "tokens": 7})
    task_b = _task(task_id="b", expected_metrics={"dur_s": 2.0, "llm_calls": 3, "tokens": 9})
    first = runner.run(task_a)
    assert first == runner.run(task_a)
    assert first != runner.run(task_b)
    assert all(r.study_id == "a" for r in first.usage_recorded)


def test_refusing_fake_runner_refuses_every_task() -> None:
    runner = RefusingFakeRunner(reason_code="driver_unimplemented", detail="no driver support yet")
    refused = runner.run(_task(task_id="web"))
    assert refused.kind == "refused"
    assert refused.reason_code == "driver_unimplemented"
    assert refused.detail == "no driver support yet"
    assert refused == fake_refusal("driver_unimplemented", "no driver support yet")
