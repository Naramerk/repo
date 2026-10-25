"""End-to-end tests for the suite harness (plan items 5+11) with fakes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.errors import BenchConfigError
from bench.fake import FakeRunner, RefusingFakeRunner
from bench.load_tasks import load_tasks
from bench.models import suite_llm_calls, suite_tokens, suite_wall_time_s
from bench.runner import BenchRunner
from bench.suite import run_suite, time_run, write_suite_artifacts

HER_SUITE_DIR = Path(__file__).resolve().parents[1] / "bench" / "tasks"


def _not_a_runner() -> object:
    class NotARunner:
        def execute(self, task: object) -> object:  # wrong method name on purpose
            return task

    return NotARunner()


def test_fake_runners_satisfy_protocol() -> None:
    assert isinstance(FakeRunner(), BenchRunner)
    assert isinstance(RefusingFakeRunner(reason_code="r", detail="d"), BenchRunner)


def test_object_without_run_does_not_satisfy_protocol() -> None:
    assert not isinstance(_not_a_runner(), BenchRunner)


def test_time_run_refuses_non_runner() -> None:
    task = load_tasks(HER_SUITE_DIR)[0]
    with pytest.raises(BenchConfigError, match="does not satisfy the BenchRunner protocol"):
        time_run(_not_a_runner(), task, "fake")  # type: ignore[arg-type]


def test_time_run_wraps_success_with_wall_time() -> None:
    task = load_tasks(HER_SUITE_DIR)[0]
    timed = time_run(FakeRunner(), task, "fake")
    assert timed.kind == "ok"
    assert timed.system_id == "fake"
    assert timed.task_id == task.task_id
    assert timed.dur_s >= 0.0


def test_time_run_wraps_refusal() -> None:
    task = load_tasks(HER_SUITE_DIR)[0]
    timed = time_run(RefusingFakeRunner(reason_code="driver_unimplemented", detail="nope"), task, "fake")
    assert timed.kind == "refused"
    assert timed.reason_code == "driver_unimplemented"


def test_e2e_fake_suite_over_her_tasks(tmp_path: Path) -> None:
    tasks = load_tasks(HER_SUITE_DIR)
    manifest_ids = [t.task_id for t in tasks]
    result = run_suite(
        FakeRunner(),
        tasks,
        system_id="fake",
        task_set_id="her_runs_final_20261023",
        task_set_version="1",
    )

    assert [entry.run.task_id for entry in result.runs] == manifest_ids
    assert all(entry.run.kind == "ok" for entry in result.runs)
    # FakeRunner replays expected quality -> every exact comparison matches.
    assert all(entry.quality is not None and entry.quality.equal for entry in result.runs)
    # Aggregates equal the HER expected metrics sums (derived, not hardcoded).
    assert suite_llm_calls(result) == sum(t.expected_metrics.llm_calls for t in tasks)
    assert suite_tokens(result) == sum(t.expected_metrics.tokens for t in tasks)
    assert suite_wall_time_s(result) >= 0.0

    write_suite_artifacts(tmp_path / "out", result)
    payload = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert payload["system_id"] == "fake"
    assert payload["task_set_id"] == "her_runs_final_20261023"
    assert payload["llm_calls"] == sum(t.expected_metrics.llm_calls for t in tasks)
    assert payload["tokens"] == sum(t.expected_metrics.tokens for t in tasks)
    assert len(payload["runs"]) == 10
    usage_lines = (tmp_path / "out" / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(usage_lines) == sum(t.expected_metrics.llm_calls for t in tasks)
    summary = (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")
    assert "Task set: her_runs_final_20261023 v1" in summary
    assert summary.count("| match |") == 10


def test_e2e_refusing_suite_over_her_tasks(tmp_path: Path) -> None:
    tasks = load_tasks(HER_SUITE_DIR)
    result = run_suite(
        RefusingFakeRunner(reason_code="driver_unimplemented", detail="web/recovery drivers not implemented"),
        tasks,
        system_id="fake-refusing",
        task_set_id="her_runs_final_20261023",
        task_set_version="1",
    )
    assert all(entry.run.kind == "refused" for entry in result.runs)
    assert all(entry.quality is None for entry in result.runs)
    assert suite_llm_calls(result) == 0
    assert suite_tokens(result) == 0

    write_suite_artifacts(tmp_path / "out", result)
    payload = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert payload["llm_calls"] == 0
    assert payload["tokens"] == 0
    assert all(run["kind"] == "refused" for run in payload["runs"])
    assert (tmp_path / "out" / "usage.jsonl").read_text(encoding="utf-8") == ""


def test_write_suite_artifacts_refuses_file_output_path(tmp_path: Path) -> None:
    tasks = load_tasks(HER_SUITE_DIR)[:1]
    result = run_suite(
        FakeRunner(),
        tasks,
        system_id="fake",
        task_set_id="her_runs_final_20261023",
        task_set_version="1",
    )
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    with pytest.raises(BenchConfigError, match="not a directory"):
        write_suite_artifacts(blocker, result)
