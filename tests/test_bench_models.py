"""Unit tests for bench.models (plan item 3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bench.models import (
    BenchTask,
    EvidenceOutcomeCount,
    QualityExpectation,
    TaskSetManifest,
)


def _quality(*, status: str = "fulfilled", exp_tasks: int = 2) -> QualityExpectation:
    return QualityExpectation(
        status=status,
        exp_tasks=exp_tasks,
        evidence=(EvidenceOutcomeCount(outcome="success", count=2),),
    )


def test_evidence_outcome_count_accepts_declared_row() -> None:
    row = EvidenceOutcomeCount(outcome="null", count=19)
    assert row.outcome == "null"
    assert row.count == 19


def test_evidence_outcome_count_rejects_empty_outcome() -> None:
    with pytest.raises(ValidationError):
        EvidenceOutcomeCount(outcome="", count=1)


def test_evidence_outcome_count_rejects_negative_count() -> None:
    with pytest.raises(ValidationError):
        EvidenceOutcomeCount(outcome="success", count=-1)


def test_evidence_outcome_count_rejects_bool_count_under_strict() -> None:
    with pytest.raises(ValidationError):
        EvidenceOutcomeCount(outcome="success", count=True)  # type: ignore[arg-type]


def test_quality_expectation_accepts_her_shaped_fields() -> None:
    quality = QualityExpectation(
        status="partial",
        exp_tasks=1,
        evidence=(
            EvidenceOutcomeCount(outcome="null", count=1),
            EvidenceOutcomeCount(outcome="success", count=1),
        ),
    )
    assert quality.status == "partial"
    assert quality.exp_tasks == 1
    assert len(quality.evidence) == 2


def test_quality_expectation_rejects_duplicate_evidence_outcomes() -> None:
    with pytest.raises(ValidationError, match="unique"):
        QualityExpectation(
            status="fulfilled",
            exp_tasks=2,
            evidence=(
                EvidenceOutcomeCount(outcome="success", count=1),
                EvidenceOutcomeCount(outcome="success", count=1),
            ),
        )


def test_quality_expectation_rejects_empty_status() -> None:
    with pytest.raises(ValidationError):
        QualityExpectation(
            status="",
            exp_tasks=0,
            evidence=(),
        )


def test_quality_expectation_rejects_negative_exp_tasks() -> None:
    with pytest.raises(ValidationError):
        QualityExpectation(
            status="timeout",
            exp_tasks=-1,
            evidence=(),
        )


def test_quality_expectation_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        QualityExpectation(
            status="fulfilled",
            exp_tasks=1,
            evidence=(),
            score=0.9,  # type: ignore[call-arg]
        )


def test_bench_task_accepts_required_fields() -> None:
    task = BenchTask(
        task_id="two_tasks",
        task_set_version="1",
        prompt="Run the two_tasks research request.",
        quality=_quality(),
    )
    assert task.task_id == "two_tasks"
    assert task.quality.exp_tasks == 2


def test_bench_task_rejects_empty_prompt() -> None:
    with pytest.raises(ValidationError):
        BenchTask(
            task_id="two_tasks",
            task_set_version="1",
            prompt="",
            quality=_quality(),
        )


def test_bench_task_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        BenchTask(
            task_id="two_tasks",
            task_set_version="1",
            prompt="x",
            quality=_quality(),
            title="invented",  # type: ignore[call-arg]
        )


def test_bench_task_is_frozen() -> None:
    task = BenchTask(
        task_id="web",
        task_set_version="1",
        prompt="web scenario",
        quality=_quality(status="graph:fulfilled"),
    )
    with pytest.raises(ValidationError):
        task.task_id = "other"  # type: ignore[misc]


def test_task_set_manifest_accepts_unique_relative_paths() -> None:
    manifest = TaskSetManifest(
        task_set_id="her_runs_final_20261023",
        task_set_version="1",
        tasks=("two_tasks.yaml", "web.yaml"),
    )
    assert manifest.tasks == ("two_tasks.yaml", "web.yaml")


def test_task_set_manifest_rejects_empty_tasks() -> None:
    with pytest.raises(ValidationError):
        TaskSetManifest(
            task_set_id="her_runs_final_20261023",
            task_set_version="1",
            tasks=(),
        )


def test_task_set_manifest_rejects_whitespace_path() -> None:
    with pytest.raises(ValidationError, match="whitespace"):
        TaskSetManifest(
            task_set_id="her_runs_final_20261023",
            task_set_version="1",
            tasks=("two tasks.yaml",),
        )


def test_task_set_manifest_rejects_duplicate_paths() -> None:
    with pytest.raises(ValidationError, match="unique"):
        TaskSetManifest(
            task_set_id="her_runs_final_20261023",
            task_set_version="1",
            tasks=("a.yaml", "a.yaml"),
        )


def test_task_set_manifest_rejects_empty_task_set_id() -> None:
    with pytest.raises(ValidationError):
        TaskSetManifest(
            task_set_id="",
            task_set_version="1",
            tasks=("a.yaml",),
        )
