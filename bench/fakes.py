"""Deterministic fake runners for harness end-to-end tests (no network)."""

from __future__ import annotations

from collections.abc import Callable

from bench.models import (
    BenchTask,
    CallCounts,
    PlanCriterionObservation,
    ProviderTokenUsage,
    QualityObservation,
    RunnerRawResult,
    RunnerRefuse,
    RunnerSuccess,
    VerdictObservation,
)

ResultFactory = Callable[[BenchTask], RunnerRawResult]


class FakeRunner:
    """In-memory runner driven by an explicit per-task factory or result map.

    Prefer ``factory`` so task ids stay in versioned config files, not in code.
    ``results`` is for unit tests that need a fixed map keyed by task_id.
    """

    def __init__(
        self,
        *,
        system_id: str,
        factory: ResultFactory | None = None,
        results: dict[str, RunnerRawResult] | None = None,
    ) -> None:
        if not system_id:
            raise ValueError("system_id must be non-empty")
        if factory is None and results is None:
            raise ValueError("FakeRunner requires factory or results")
        if factory is not None and results is not None:
            raise ValueError("FakeRunner accepts factory or results, not both")
        self._system_id = system_id
        self._factory = factory
        self._results = None if results is None else dict(results)

    @property
    def system_id(self) -> str:
        return self._system_id

    async def run_task(self, task: BenchTask) -> RunnerRawResult:
        if self._factory is not None:
            return self._factory(task)
        assert self._results is not None
        if task.task_id not in self._results:
            return RunnerRefuse(
                kind="refused",
                reason_code="fake_task_not_configured",
                detail=f"FakeRunner {self._system_id!r} has no result for {task.task_id!r}",
            )
        return self._results[task.task_id]


def make_success(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    llm_calls: int,
    tool_calls: int,
    report_fields_present: tuple[str, ...],
    plan_criteria: tuple[PlanCriterionObservation, ...],
    verdicts: tuple[VerdictObservation, ...],
) -> RunnerSuccess:
    return RunnerSuccess(
        kind="success",
        tokens=ProviderTokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        calls=CallCounts(llm_calls=llm_calls, tool_calls=tool_calls),
        quality=QualityObservation(
            verdicts=verdicts,
            plan_criteria=plan_criteria,
            report_fields_present=report_fields_present,
        ),
    )


def meeting_expectations_factory(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    llm_calls: int,
    tool_calls: int,
) -> ResultFactory:
    """Build success payloads that meet each task's configured quality expectation."""

    def _factory(task: BenchTask) -> RunnerRawResult:
        criteria = tuple(
            PlanCriterionObservation(
                criterion_id=c.criterion_id,
                status=c.required_status,
            )
            for c in task.quality.plan_criteria
        )
        return make_success(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            report_fields_present=tuple(task.quality.required_report_fields),
            plan_criteria=criteria,
            verdicts=(
                VerdictObservation(question_id="q1", disposition="answered"),
            ),
        )

    return _factory
