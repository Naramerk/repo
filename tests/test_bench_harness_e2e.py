"""End-to-end harness with fake runners for both systems."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.adapters.new_runner import NewCoScientistRunner, NewRunnerConfig
from bench.adapters.old_runner import OldCoScientistRunner, OldRunnerConfig
from bench.errors import BenchConfigError
from bench.fakes import FakeRunner, make_success, meeting_expectations_factory
from bench.harness import run_suite, run_suite_and_write
from bench.models import (
    PlanCriterionObservation,
    RunnerRefuse,
    TimedRunOk,
    TimedRunRefused,
    VerdictObservation,
)
from bench.protocol import BenchRunner

REPO_MANIFEST = Path(__file__).resolve().parents[1] / "bench" / "tasks" / "v1" / "manifest.yaml"


@pytest.mark.asyncio
async def test_suite_with_fake_runners_meeting_expectations() -> None:
    old: BenchRunner = FakeRunner(
        system_id="old",
        factory=meeting_expectations_factory(
            prompt_tokens=10,
            completion_tokens=5,
            llm_calls=1,
            tool_calls=1,
        ),
    )
    new: BenchRunner = FakeRunner(
        system_id="new",
        factory=meeting_expectations_factory(
            prompt_tokens=8,
            completion_tokens=4,
            llm_calls=1,
            tool_calls=0,
        ),
    )
    suite = await run_suite(
        manifest_path=REPO_MANIFEST,
        old_runner=old,
        new_runner=new,
    )
    assert suite.task_set_id == "easy_research_v1"
    assert len(suite.comparisons) == 3
    for cmp_ in suite.comparisons:
        assert isinstance(cmp_.old, TimedRunOk)
        assert isinstance(cmp_.new, TimedRunOk)
        assert cmp_.old.tokens.total_tokens == 15
        assert cmp_.new.tokens.total_tokens == 12
        assert cmp_.old.quality_score.report_fields_missing == ()
        assert cmp_.new.quality_score.plan_criteria_unmet == ()
        assert cmp_.old.wall_time_ms >= 0.0


@pytest.mark.asyncio
async def test_write_json_and_markdown(tmp_path: Path) -> None:
    old = FakeRunner(
        system_id="old",
        factory=meeting_expectations_factory(
            prompt_tokens=1,
            completion_tokens=1,
            llm_calls=1,
            tool_calls=0,
        ),
    )
    new = FakeRunner(
        system_id="new",
        factory=meeting_expectations_factory(
            prompt_tokens=2,
            completion_tokens=2,
            llm_calls=2,
            tool_calls=1,
        ),
    )
    suite = await run_suite_and_write(
        manifest_path=REPO_MANIFEST,
        old_runner=old,
        new_runner=new,
        out_dir=tmp_path,
    )
    result_path = tmp_path / "result.json"
    summary_path = tmp_path / "summary.md"
    assert result_path.is_file()
    assert summary_path.is_file()
    data = json.loads(result_path.read_text(encoding="utf-8"))
    assert data["task_set_id"] == suite.task_set_id
    assert len(data["comparisons"]) == 3
    text = summary_path.read_text(encoding="utf-8")
    assert "lit_summary_easy" in text
    assert "tokens=" in text


@pytest.mark.asyncio
async def test_refuse_when_fake_missing_task() -> None:
    old = FakeRunner(
        system_id="old",
        results={
            "lit_summary_easy": make_success(
                prompt_tokens=1,
                completion_tokens=1,
                llm_calls=1,
                tool_calls=0,
                report_fields_present=("summary",),
                plan_criteria=(),
                verdicts=(),
            ),
        },
    )
    new = FakeRunner(
        system_id="new",
        factory=meeting_expectations_factory(
            prompt_tokens=1,
            completion_tokens=1,
            llm_calls=1,
            tool_calls=0,
        ),
    )
    suite = await run_suite(
        manifest_path=REPO_MANIFEST,
        old_runner=old,
        new_runner=new,
    )
    by_id = {c.task_id: c for c in suite.comparisons}
    assert isinstance(by_id["lit_summary_easy"].old, TimedRunOk)
    assert isinstance(by_id["hypothesis_sketch_easy"].old, TimedRunRefused)
    assert by_id["hypothesis_sketch_easy"].old.reason_code == "fake_task_not_configured"


@pytest.mark.asyncio
async def test_distinct_system_ids_required() -> None:
    r = FakeRunner(
        system_id="same",
        factory=meeting_expectations_factory(
            prompt_tokens=1,
            completion_tokens=1,
            llm_calls=1,
            tool_calls=0,
        ),
    )
    with pytest.raises(BenchConfigError, match="distinct system_id"):
        await run_suite(manifest_path=REPO_MANIFEST, old_runner=r, new_runner=r)


@pytest.mark.asyncio
async def test_new_runner_unwired_typed_refuse() -> None:
    runner = NewCoScientistRunner(NewRunnerConfig(system_id="new", entrypoint=None))
    from bench.load_tasks import load_task_set

    _, tasks = load_task_set(REPO_MANIFEST)
    raw = await runner.run_task(tasks[0])
    assert isinstance(raw, RunnerRefuse)
    assert raw.reason_code == "new_runner_unwired"


@pytest.mark.asyncio
async def test_old_runner_missing_checkout_typed_refuse(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_checkout"
    runner = OldCoScientistRunner(
        OldRunnerConfig(
            system_id="old",
            checkout_path=str(missing),
            expected_branch="dev_1",
            verify_branch=True,
            entrypoint="adapter:run_task",
        )
    )
    from bench.load_tasks import load_task_set

    _, tasks = load_task_set(REPO_MANIFEST)
    raw = await runner.run_task(tasks[0])
    assert isinstance(raw, RunnerRefuse)
    assert raw.reason_code == "old_adapter_unavailable"


@pytest.mark.asyncio
async def test_old_runner_verify_branch_without_git(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    runner = OldCoScientistRunner(
        OldRunnerConfig(
            system_id="old",
            checkout_path=str(checkout),
            expected_branch="dev_1",
            verify_branch=True,
            entrypoint="adapter:run_task",
        )
    )
    from bench.load_tasks import load_task_set

    _, tasks = load_task_set(REPO_MANIFEST)
    raw = await runner.run_task(tasks[0])
    assert isinstance(raw, RunnerRefuse)
    assert raw.reason_code == "old_adapter_unavailable"
    assert "no .git" in raw.detail


@pytest.mark.asyncio
async def test_new_runner_injected_callable() -> None:
    async def _run(task):
        return make_success(
            prompt_tokens=3,
            completion_tokens=4,
            llm_calls=1,
            tool_calls=1,
            report_fields_present=tuple(task.quality.required_report_fields),
            plan_criteria=tuple(
                PlanCriterionObservation(
                    criterion_id=c.criterion_id,
                    status=c.required_status,
                )
                for c in task.quality.plan_criteria
            ),
            verdicts=(
                VerdictObservation(question_id="q1", disposition="answered"),
            ),
        )

    new = NewCoScientistRunner(
        NewRunnerConfig(system_id="new", entrypoint=None),
        injected=_run,
    )
    old = FakeRunner(
        system_id="old",
        factory=meeting_expectations_factory(
            prompt_tokens=1,
            completion_tokens=1,
            llm_calls=1,
            tool_calls=0,
        ),
    )
    suite = await run_suite(
        manifest_path=REPO_MANIFEST,
        old_runner=old,
        new_runner=new,
    )
    assert all(isinstance(c.new, TimedRunOk) for c in suite.comparisons)
    assert suite.comparisons[0].new.tokens.total_tokens == 7


def test_protocol_runtime_checkable() -> None:
    runner = FakeRunner(
        system_id="x",
        factory=meeting_expectations_factory(
            prompt_tokens=1,
            completion_tokens=1,
            llm_calls=1,
            tool_calls=0,
        ),
    )
    assert isinstance(runner, BenchRunner)
