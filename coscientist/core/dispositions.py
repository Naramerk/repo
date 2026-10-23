"""Question dispositions (PR 2: all not_attempted; PR 6/8 add verdict paths)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coscientist.core.domain import Task, TaskOutcome, TaskState
from coscientist.core.question_tasks import known_questions, question_tasks
from coscientist.core.reasons import Disposition, NotAttemptedReason, TaskReason


def _task_by_key(tasks: Mapping[str, Task]) -> dict[str, Task]:
    return {t.spec.task_key: t for t in tasks.values()}


def _reasons_for(tasks: list[Task]) -> list[TaskReason]:
    out: list[TaskReason] = []
    for t in tasks:
        if t.reason is None:
            continue
        r = t.reason if isinstance(t.reason, TaskReason) else TaskReason(str(t.reason))
        if r not in out:
            out.append(r)
    return out


def _not_attempted_for_covering(
    covering: list[Task],
) -> tuple[NotAttemptedReason, list[TaskReason]]:
    if not covering:
        return NotAttemptedReason.NO_TASK, []
    done = [t for t in covering if t.state == TaskState.DONE]
    if not done:
        return NotAttemptedReason.NO_TASK, _reasons_for(covering)
    if any(t.outcome in {TaskOutcome.SUCCESS, TaskOutcome.PARTIAL} for t in done):
        # PR 6/8: compute answered/… from claims. Core has no claim pipeline.
        return NotAttemptedReason.NO_COUNTING_CLAIMS, _reasons_for(done)
    if all(t.outcome == TaskOutcome.FAILED for t in done):
        return NotAttemptedReason.TASKS_FAILED, _reasons_for(done)
    return NotAttemptedReason.TASKS_SKIPPED, _reasons_for(done)


def dispositions(
    *,
    tasks: Mapping[str, Task],
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Every question → not_attempted + NotAttemptedReason (PR 2).

    TODO(PR 6/8): answered/refuted/survived/inconclusive from counting claims;
    explicit QuestionDisposed; ill_posed from refuted premises.
    """
    qmap = known_questions(plans)
    cover = question_tasks(plans)
    by_key = _task_by_key(tasks)
    result: dict[str, dict[str, Any]] = {}
    for qid, text in qmap.items():
        keys = cover.get(qid, set())
        covering = [by_key[k] for k in sorted(keys) if k in by_key]
        reason, detail = _not_attempted_for_covering(covering)
        result[qid] = {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": reason.value,
            "reason_detail": [r.value for r in detail],
            "formulation": text,
        }
    return result
