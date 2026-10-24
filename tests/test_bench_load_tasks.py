"""Task-set loading from versioned YAML (no hardcoded tasks)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bench.errors import BenchConfigError
from bench.load_tasks import load_task_set

REPO_MANIFEST = Path(__file__).resolve().parents[1] / "bench" / "tasks" / "v1" / "manifest.yaml"


def test_load_shipped_v1_task_set() -> None:
    manifest, tasks = load_task_set(REPO_MANIFEST)
    assert manifest.task_set_id == "easy_research_v1"
    assert manifest.task_set_version == "1"
    assert len(tasks) == len(manifest.tasks)
    assert {t.task_id for t in tasks} == {
        "lit_summary_easy",
        "hypothesis_sketch_easy",
        "fact_check_easy",
    }
    for task in tasks:
        assert task.task_set_version == "1"
        assert task.prompt.strip()
        assert task.quality.required_report_fields
        assert task.quality.plan_criteria


def test_reject_absolute_task_path(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "task_set_id: t\ntask_set_version: \"1\"\ntasks:\n  - /etc/passwd\n",
        encoding="utf-8",
    )
    with pytest.raises(BenchConfigError, match="relative"):
        load_task_set(manifest)


def test_reject_version_mismatch(tmp_path: Path) -> None:
    task = tmp_path / "a.yaml"
    task.write_text(
        "\n".join(
            [
                'task_id: a',
                'task_set_version: "2"',
                "title: t",
                "prompt: p",
                "quality:",
                "  required_report_fields: [summary]",
                "  plan_criteria: []",
            ]
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        'task_set_id: t\ntask_set_version: "1"\ntasks:\n  - a.yaml\n',
        encoding="utf-8",
    )
    with pytest.raises(BenchConfigError, match="task_set_version"):
        load_task_set(manifest)
