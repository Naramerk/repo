"""Unit tests for bench.models (plan item 3)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from bench.models import (
    BenchTask,
    EvidenceOutcomeCount,
    QualityExpectation,
    TaskSetManifest,
)


def _quality_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "fulfilled",
        "exp_tasks": 2,
        "evidence": ({"outcome": "success", "count": 2},),
    }
    payload.update(overrides)
    return payload


def _task_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": "two_tasks",
        "task_set_version": "1",
        "prompt": "Run the two_tasks research request.",
        "quality": _quality_payload(),
    }
    payload.update(overrides)
    return payload


def _manifest_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_set_id": "her_runs_final_20261023",
        "task_set_version": "1",
        "tasks": ("two_tasks.yaml", "web.yaml"),
    }
    payload.update(overrides)
    return payload


def test_evidence_outcome_count_accepts_declared_row() -> None:
    row = EvidenceOutcomeCount.model_validate({"outcome": "null", "count": 19})
    assert row.outcome == "null"
    assert row.count == 19


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"outcome": "", "count": 1}, "at least 1 character"),
        ({"outcome": "success", "count": -1}, "greater than or equal to 0"),
        ({"outcome": "success", "count": True}, "valid integer"),
    ],
    ids=["empty_outcome", "negative_count", "bool_count"],
)
def test_evidence_outcome_count_rejects(payload: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        EvidenceOutcomeCount.model_validate(payload)


def test_quality_expectation_accepts_her_shaped_fields() -> None:
    quality = QualityExpectation.model_validate(
        {
            "status": "partial",
            "exp_tasks": 1,
            "evidence": (
                {"outcome": "null", "count": 1},
                {"outcome": "success", "count": 1},
            ),
        }
    )
    assert quality.status == "partial"
    assert quality.exp_tasks == 1
    assert len(quality.evidence) == 2


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {
                "status": "fulfilled",
                "exp_tasks": 2,
                "evidence": (
                    {"outcome": "success", "count": 1},
                    {"outcome": "success", "count": 1},
                ),
            },
            "unique",
        ),
        ({"status": "", "exp_tasks": 0, "evidence": ()}, "at least 1 character"),
        (
            {"status": "timeout", "exp_tasks": -1, "evidence": ()},
            "greater than or equal to 0",
        ),
        (
            {"status": "fulfilled", "exp_tasks": 1, "evidence": (), "score": 0.9},
            "Extra inputs are not permitted",
        ),
    ],
    ids=["duplicate_evidence", "empty_status", "negative_exp_tasks", "extra_field"],
)
def test_quality_expectation_rejects(payload: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match) as ei:
        QualityExpectation.model_validate(payload)
    if "unique" in match:
        assert "QualityExpectation.evidence" in ei.value.errors()[0]["msg"]


def test_bench_task_accepts_required_fields() -> None:
    task = BenchTask.model_validate(_task_payload())
    assert task.task_id == "two_tasks"
    assert task.quality.exp_tasks == 2


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (_task_payload(prompt=""), "at least 1 character"),
        (_task_payload(title="invented"), "Extra inputs are not permitted"),
    ],
    ids=["empty_prompt", "extra_field"],
)
def test_bench_task_rejects(payload: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        BenchTask.model_validate(payload)


def test_bench_task_is_frozen() -> None:
    task = BenchTask.model_validate(
        _task_payload(
            task_id="web",
            prompt="web scenario",
            quality=_quality_payload(status="graph:fulfilled"),
        )
    )
    field_name = "task_id"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(task, field_name, "other")


def test_task_set_manifest_accepts_unique_relative_paths() -> None:
    manifest = TaskSetManifest.model_validate(_manifest_payload())
    assert manifest.tasks == ("two_tasks.yaml", "web.yaml")


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (_manifest_payload(tasks=()), "at least 1 item"),
        (_manifest_payload(tasks=("two tasks.yaml",)), "whitespace"),
        (_manifest_payload(tasks=("/etc/x.yaml",)), "absolute"),
        (_manifest_payload(tasks=("../x.yaml",)), r"\.\."),
        (_manifest_payload(tasks=("subdir/../x.yaml",)), r"\.\."),
        (_manifest_payload(tasks=("a.yaml", "a.yaml")), "unique"),
        (_manifest_payload(task_set_id=""), "at least 1 character"),
    ],
    ids=[
        "empty_tasks",
        "whitespace_path",
        "absolute_path",
        "parent_segment",
        "nested_parent_segment",
        "duplicate_paths",
        "empty_task_set_id",
    ],
)
def test_task_set_manifest_rejects(payload: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        TaskSetManifest.model_validate(payload)
