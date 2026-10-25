"""Validation of the shipped HER task set (plan item 6).

The YAML under ``bench/tasks/`` is versioned data: these tests pin it to the
``runs_final_20261023`` extract values recorded in docs/plan/benchmark-harness.md
so silent edits to prompts or expected outcomes fail CI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from bench.models import BenchTask, TaskSetManifest

TASKS_DIR = Path(__file__).resolve().parents[1] / "bench" / "tasks"
LEGACY_PATH_PREFIX = "/Users/nargiza/CoScientist_dev-dev_1-2026-10-22"

# (scenario, status, exp_tasks, evidence, dur_s, llm_calls, tokens, driver)
# Source: runs_final_20261023 metrics.json rows == plan table.
HER_ROWS: tuple[tuple[str, str, int, tuple[tuple[str, int], ...], float, int, int, str | None], ...] = (
    ("two_tasks", "fulfilled", 2, (("success", 2),), 1172.0, 53, 1097723, None),
    ("hypothesis_check", "fulfilled", 1, (("success", 1),), 906.0, 53, 868394, None),
    ("borshevik", "partial", 1, (("null", 1), ("success", 1)), 2566.0, 99, 3192669, None),
    ("alembic_nomcp", "fulfilled", 1, (("success", 1),), 1019.0, 76, 1694593, None),
    ("microfluidics", "timeout", 0, (("null", 19),), 2402.0, 111, 2613368, None),
    ("article_small", "partial", 1, (("success", 1),), 641.0, 37, 600791, None),
    # web llm_calls = 55 provider calls per the plan's † footnote (never the
    # undercounted top-level llm_calls=0 of the old webdriver run).
    ("web", "graph:fulfilled", 2, (("success", 2),), 835.0, 55, 1115909, "web"),
    ("recovery", "graph:fulfilled", 2, (("success", 2),), 902.0, 59, 1003049, "recovery"),
    ("file_handoff", "unfulfilled", 6, (("success", 2),), 1443.0, 93, 2133695, None),
    ("recovery2", "graph:fulfilled", 2, (("success", 2),), 955.0, 60, 1049306, "recovery"),
)
SCENARIOS = tuple(row[0] for row in HER_ROWS)


def _validate_task(raw: dict[str, Any]) -> BenchTask:
    # bench.models are strict (tuples, not lists) — same coercion bench.load_tasks
    # applies when reading YAML (plan item 7).
    coerced = dict(raw)
    coerced["inputs"] = tuple(raw.get("inputs", ()))
    quality = dict(raw["quality"])
    quality["evidence"] = tuple(quality["evidence"])
    coerced["quality"] = quality
    return BenchTask.model_validate(coerced)


def _load_task_yaml(scenario: str) -> BenchTask:
    raw = yaml.safe_load((TASKS_DIR / f"{scenario}.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return _validate_task(raw)


def test_manifest_declares_all_ten_scenarios() -> None:
    raw = yaml.safe_load((TASKS_DIR / "manifest.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    manifest = TaskSetManifest.model_validate({**raw, "tasks": tuple(raw["tasks"])})
    assert manifest.task_set_id == "her_runs_final_20261023"
    assert manifest.task_set_version == "1"
    assert manifest.tasks == tuple(f"{scenario}.yaml" for scenario in SCENARIOS)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_task_file_matches_extract_row(scenario: str) -> None:
    row = next(r for r in HER_ROWS if r[0] == scenario)
    _, status, exp_tasks, evidence, dur_s, llm_calls, tokens, driver = row
    task = _load_task_yaml(scenario)
    assert task.task_id == scenario
    assert task.task_set_version == "1"
    assert task.quality.status == status
    assert task.quality.exp_tasks == exp_tasks
    assert tuple((e.outcome, e.count) for e in task.quality.evidence) == evidence
    assert task.expected_metrics.dur_s == dur_s
    assert task.expected_metrics.llm_calls == llm_calls
    assert task.expected_metrics.tokens == tokens
    assert task.driver == driver


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_prompt_is_stripped_of_legacy_absolute_paths(scenario: str) -> None:
    task = _load_task_yaml(scenario)
    assert LEGACY_PATH_PREFIX not in task.prompt


def test_hypothesis_check_prompt_points_at_copied_inputs() -> None:
    task = _load_task_yaml("hypothesis_check")
    assert "bench/tasks/data/hypothesis_check/reference.csv" in task.prompt
    assert "bench/tasks/data/hypothesis_check/candidates.csv" in task.prompt
    assert task.inputs == (
        "data/hypothesis_check/reference.csv",
        "data/hypothesis_check/candidates.csv",
    )


def test_file_handoff_prompt_points_at_copied_inputs() -> None:
    task = _load_task_yaml("file_handoff")
    assert "bench/tasks/data/file_handoff/inputs/active_IC50_1.csv" in task.prompt
    assert "bench/tasks/data/file_handoff/inputs/novel_molecules_dyslipidemia.csv" in task.prompt
    assert task.inputs == (
        "data/file_handoff/inputs/active_IC50_1.csv",
        "data/file_handoff/inputs/novel_molecules_dyslipidemia.csv",
    )


@pytest.mark.parametrize(
    "scenario",
    ["two_tasks", "web", "recovery", "recovery2"],
)
def test_two_task_prompt_shared_by_driver_scenarios(scenario: str) -> None:
    # Extract fact: two_tasks / web / recovery / recovery2 ran the same prompt;
    # driver-based scenarios differ only in the driver field.
    base = _load_task_yaml("two_tasks").prompt
    assert _load_task_yaml(scenario).prompt == base


@pytest.mark.parametrize(
    "relative",
    [
        "data/hypothesis_check/reference.csv",
        "data/hypothesis_check/candidates.csv",
        "data/file_handoff/inputs/active_IC50_1.csv",
        "data/file_handoff/inputs/novel_molecules_dyslipidemia.csv",
    ],
)
def test_declared_input_data_files_exist(relative: str) -> None:
    path = TASKS_DIR / relative
    assert path.is_file(), f"missing copied HER input: {path}"
    assert path.stat().st_size > 0


def test_two_tasks_task_declares_no_inputs_or_driver() -> None:
    task = _load_task_yaml("two_tasks")
    assert task.inputs == ()
    assert task.driver is None


def _her_yaml_round_trip(scenario: str) -> dict[str, Any]:
    raw = yaml.safe_load((TASKS_DIR / f"{scenario}.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_prompt_yaml_block_keeps_trailing_newline() -> None:
    raw = _her_yaml_round_trip("two_tasks")
    prompt = raw["prompt"]
    assert isinstance(prompt, str)
    assert prompt.endswith("\n")
    assert not prompt.endswith("\n\n")
    assert "\r" not in prompt
