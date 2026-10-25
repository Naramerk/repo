"""Deterministic fake runners driven purely by loaded task data (plan item 10).

No scenario ids or expected values are hardcoded here: every output is a pure
function of the ``BenchTask`` handed in. The fake replays the task's declared
expected quality and ``expected_metrics`` as if they had been observed, which
exercises the whole harness (timing, accounting, compare, artifacts) without a
live system. It is a fake — never a measurement.
"""

from __future__ import annotations

from bench.errors import BenchConfigError
from bench.models import (
    BenchTask,
    QualityObservation,
    RunnerRefuse,
    RunnerSuccess,
    UsageRecorded,
)
from coscientist.llm.types import Usage


def fake_quality_observation(task: BenchTask) -> QualityObservation:
    """Replay the task's expected quality; ``record_result`` = sum of evidence counts."""
    return QualityObservation(
        status=task.quality.status,
        exp_tasks=task.quality.exp_tasks,
        evidence=task.quality.evidence,
        record_result=sum(row.count for row in task.quality.evidence),
    )


def fake_usage_recorded(task: BenchTask) -> tuple[UsageRecorded, ...]:
    """Replay ``expected_metrics`` as provider usage: exactly ``llm_calls`` records
    whose token total is exactly ``tokens``.

    Deterministic split: all tokens are prompt tokens, ``tokens // llm_calls``
    each, the remainder spread one token per record over the first
    ``tokens % llm_calls`` records; completion tokens and latency are zero.
    ``llm_calls == 0`` with ``tokens > 0`` is contradictory data and is refused.
    """
    calls = task.expected_metrics.llm_calls
    tokens = task.expected_metrics.tokens
    if calls == 0:
        if tokens != 0:
            raise BenchConfigError(
                f"task {task.task_id!r} expected_metrics cannot be replayed: "
                f"llm_calls=0 but tokens={tokens}"
            )
        return ()
    base, remainder = divmod(tokens, calls)
    return tuple(
        UsageRecorded(
            study_id=task.task_id,
            task_id=task.task_id,
            attempt_id=None,
            role="fake",
            usage=Usage(prompt_tokens=base + (1 if i < remainder else 0), completion_tokens=0, latency_ms=0.0),
        )
        for i in range(calls)
    )


def fake_success(task: BenchTask) -> RunnerSuccess:
    """Successful run payload replayed from the task data."""
    return RunnerSuccess(
        kind="success",
        usage_recorded=fake_usage_recorded(task),
        quality=fake_quality_observation(task),
    )


def fake_refusal(reason_code: str, detail: str) -> RunnerRefuse:
    """Typed refusal payload for refusing fakes."""
    return RunnerRefuse(kind="refused", reason_code=reason_code, detail=detail)


class FakeRunner:
    """Runner whose result is a pure function of the loaded task data."""

    def run(self, task: BenchTask) -> RunnerSuccess:
        return fake_success(task)


class RefusingFakeRunner:
    """Runner that typed-refuses every task with the configured reason."""

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail

    def run(self, task: BenchTask) -> RunnerRefuse:
        return fake_refusal(self.reason_code, self.detail)
