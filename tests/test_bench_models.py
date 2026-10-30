"""Unit tests for bench.models (plan item 3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bench.models import (
    BenchTask,
    EvidenceOutcomeCount,
    ExpectedMetrics,
    QualityExpectation,
    TaskSetManifest,
)


def _quality_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "fulfilled",
        "exp_tasks": 2,
        "evidence": ({"outcome": "success", "count": 2},),
    }
    payload.update(overrides)
    return payload


def _metrics_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"dur_s": 1172.0, "llm_calls": 53, "tokens": 1097723}
    payload.update(overrides)
    return payload


def _task_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_id": "two_tasks",
        "task_set_version": "1",
        "prompt": "Run the two_tasks research request.",
        "expected_metrics": _metrics_payload(),
        "quality": _quality_payload(),
    }
    payload.update(overrides)
    return payload


def _manifest_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
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
        ({"outcome": "", "count": 1}, r"String should have at least 1 character"),
        (
            {"outcome": "success", "count": -1},
            r"Input should be greater than or equal to 0",
        ),
        ({"outcome": "success", "count": True}, r"Input should be a valid integer"),
    ],
    ids=["empty_outcome", "negative_count", "bool_count"],
)
def test_evidence_outcome_count_rejects(payload: dict[str, object], match: str) -> None:
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
            r"QualityExpectation\.evidence outcomes must be unique",
        ),
        (
            {"status": "", "exp_tasks": 0, "evidence": ()},
            r"String should have at least 1 character",
        ),
        (
            {"status": "timeout", "exp_tasks": -1, "evidence": ()},
            r"Input should be greater than or equal to 0",
        ),
        (
            {"status": "fulfilled", "exp_tasks": 1, "evidence": (), "score": 0.9},
            r"Extra inputs are not permitted",
        ),
    ],
    ids=["duplicate_evidence", "empty_status", "negative_exp_tasks", "extra_field"],
)
def test_quality_expectation_rejects(payload: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        QualityExpectation.model_validate(payload)


def test_bench_task_accepts_required_fields() -> None:
    task = BenchTask.model_validate(_task_payload())
    assert task.task_id == "two_tasks"
    assert task.quality.exp_tasks == 2
    assert task.driver is None
    assert task.inputs == ()
    assert task.expected_metrics.dur_s == 1172.0
    assert task.expected_metrics.llm_calls == 53
    assert task.expected_metrics.tokens == 1097723


def test_bench_task_accepts_driver_and_inputs() -> None:
    task = BenchTask.model_validate(
        _task_payload(
            task_id="recovery",
            driver="recovery",
            inputs=("data/recovery/inputs/x.csv",),
        )
    )
    assert task.driver == "recovery"
    assert task.inputs == ("data/recovery/inputs/x.csv",)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (_task_payload(driver="cli"), r"Input should be 'web' or 'recovery'"),
        (_task_payload(inputs=("data/a.csv", "data/a.csv")), r"BenchTask\.inputs paths must be unique"),
        (
            _task_payload(inputs=("/etc/a.csv",)),
            r"BenchTask\.inputs entries must be relative paths \(got absolute: '/etc/a\.csv'\)",
        ),
        (
            _task_payload(inputs=("../a.csv",)),
            r"BenchTask\.inputs entries must not contain '\.\.' \(got '\.\./a\.csv'\)",
        ),
        (_task_payload(inputs=("data/a b.csv",)), r"without whitespace"),
    ],
    ids=["bad_driver", "duplicate_inputs", "absolute_input", "parent_input", "whitespace_input"],
)
def test_bench_task_rejects_driver_or_inputs(payload: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        BenchTask.model_validate(payload)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (_metrics_payload(dur_s=-1.0), r"Input should be greater than or equal to 0"),
        (_metrics_payload(llm_calls=-1), r"Input should be greater than or equal to 0"),
        (_metrics_payload(tokens=-1), r"Input should be greater than or equal to 0"),
        (_metrics_payload(cost_rub=1.0), r"Extra inputs are not permitted"),
    ],
    ids=["negative_dur", "negative_calls", "negative_tokens", "extra_field"],
)
def test_expected_metrics_rejects(payload: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        ExpectedMetrics.model_validate(payload)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (_task_payload(prompt=""), r"String should have at least 1 character"),
        (_task_payload(title="invented"), r"Extra inputs are not permitted"),
    ],
    ids=["empty_prompt", "extra_field"],
)
def test_bench_task_rejects(payload: dict[str, object], match: str) -> None:
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
    with pytest.raises(ValidationError, match=r"Instance is frozen"):
        setattr(task, field_name, "other")


def test_task_set_manifest_accepts_unique_relative_paths() -> None:
    manifest = TaskSetManifest.model_validate(_manifest_payload())
    assert manifest.tasks == ("two_tasks.yaml", "web.yaml")


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            _manifest_payload(tasks=()),
            r"Tuple should have at least 1 item after validation, not 0",
        ),
        (
            _manifest_payload(tasks=("two tasks.yaml",)),
            r"TaskSetManifest\.tasks entries must be non-empty paths without whitespace",
        ),
        (
            _manifest_payload(tasks=("/etc/x.yaml",)),
            r"TaskSetManifest\.tasks entries must be relative paths \(got absolute: '/etc/x\.yaml'\)",
        ),
        (
            _manifest_payload(tasks=("../x.yaml",)),
            r"TaskSetManifest\.tasks entries must not contain '\.\.' \(got '\.\./x\.yaml'\)",
        ),
        (
            _manifest_payload(tasks=("subdir/../x.yaml",)),
            r"TaskSetManifest\.tasks entries must not contain '\.\.' \(got 'subdir/\.\./x\.yaml'\)",
        ),
        (
            _manifest_payload(tasks=("a.yaml", "a.yaml")),
            r"TaskSetManifest\.tasks paths must be unique",
        ),
        (
            _manifest_payload(task_set_id=""),
            r"String should have at least 1 character",
        ),
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
def test_task_set_manifest_rejects(payload: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        TaskSetManifest.model_validate(payload)
