"""Unit tests for bench.quality exact compare (plan item 8)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from bench.models import BenchTask, QualityComparison, QualityObservation
from bench.quality import compare_quality


def _task(**quality_overrides: Any) -> BenchTask:
    quality: dict[str, Any] = {
        "status": "partial",
        "exp_tasks": 1,
        "evidence": (
            {"outcome": "null", "count": 1},
            {"outcome": "success", "count": 1},
        ),
    }
    quality.update(quality_overrides)
    return BenchTask.model_validate(
        {
            "task_id": "borshevik",
            "task_set_version": "1",
            "prompt": "prompt",
            "expected_metrics": {"dur_s": 1.0, "llm_calls": 1, "tokens": 1},
            "quality": quality,
        }
    )


def _observed(**overrides: Any) -> QualityObservation:
    payload: dict[str, Any] = {
        "status": "partial",
        "exp_tasks": 1,
        "evidence": (
            {"outcome": "null", "count": 1},
            {"outcome": "success", "count": 1},
        ),
        "record_result": 2,
    }
    payload.update(overrides)
    return QualityObservation.model_validate(payload)


def test_exact_match_is_equal() -> None:
    comparison = compare_quality(_task(), _observed())
    assert comparison.equal
    assert comparison.status_equal
    assert comparison.exp_tasks_equal
    assert comparison.evidence_equal
    assert comparison.task_id == "borshevik"


def test_record_result_not_compared() -> None:
    assert compare_quality(_task(), _observed(record_result=99)).equal


def test_status_mismatch_not_equal() -> None:
    comparison = compare_quality(_task(), _observed(status="fulfilled"))
    assert not comparison.equal
    assert not comparison.status_equal
    assert comparison.exp_tasks_equal


def test_exp_tasks_mismatch_not_equal() -> None:
    comparison = compare_quality(_task(), _observed(exp_tasks=2))
    assert not comparison.equal
    assert not comparison.exp_tasks_equal
    assert comparison.status_equal


def test_evidence_count_mismatch_not_equal() -> None:
    comparison = compare_quality(
        _task(), _observed(evidence=({"outcome": "null", "count": 2}, {"outcome": "success", "count": 1}))
    )
    assert not comparison.equal
    assert not comparison.evidence_equal


def test_evidence_order_is_significant() -> None:
    comparison = compare_quality(
        _task(), _observed(evidence=({"outcome": "success", "count": 1}, {"outcome": "null", "count": 1}))
    )
    assert not comparison.equal
    assert not comparison.evidence_equal


def test_extra_evidence_row_not_equal() -> None:
    comparison = compare_quality(
        _task(),
        _observed(
            evidence=(
                {"outcome": "null", "count": 1},
                {"outcome": "success", "count": 1},
                {"outcome": "failed", "count": 1},
            )
        ),
    )
    assert not comparison.equal


def test_quality_comparison_model_rejects_inconsistent_equal() -> None:
    with pytest.raises(ValidationError, match=r"QualityComparison\.equal must equal the conjunction"):
        QualityComparison.model_validate(
            {
                "task_id": "t",
                "status_equal": False,
                "exp_tasks_equal": True,
                "evidence_equal": True,
                "equal": True,
            }
        )


def test_quality_comparison_model_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QualityComparison.model_validate(
            {
                "task_id": "t",
                "status_equal": True,
                "exp_tasks_equal": True,
                "evidence_equal": True,
                "equal": True,
                "score": 0.9,
            }
        )
