"""Question dispositions (PR 2: all not_attempted; PR 6/8 add verdict paths)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coscientist.core.domain import Task, TaskState
from coscientist.core.question_tasks import known_questions, question_tasks
from coscientist.core.reasons import Disposition, NotAttemptedReason, TaskReason


def _task_by_key(tasks: Mapping[str, Task]) -> dict[str, Task]:
    return {t.spec.task_key: t for t in tasks.values()}


def _reasons_for(tasks: list[Task]) -> list[TaskReason]:
    out: list[TaskReason] = []
    for t in tasks:
        if t.reason is None:
            continue
        if isinstance(t.reason, TaskReason):
            r = t.reason
        else:
            try:
                r = TaskReason(str(t.reason))
            except ValueError:
                continue
        if r not in out:
            out.append(r)
    return out


def not_attempted_reason(covering_outcomes: list[str | None]) -> str:
    """Map covering task outcomes → NotAttemptedReason value (oracle API)."""
    if not covering_outcomes:
        return NotAttemptedReason.NO_TASK.value
    done = [o for o in covering_outcomes if o is not None]
    if not done:
        return NotAttemptedReason.NO_TASK.value
    if any(o in ("SUCCESS", "PARTIAL") for o in done):
        return NotAttemptedReason.NO_COUNTING_CLAIMS.value
    if all(o == "FAILED" for o in done):
        return NotAttemptedReason.TASKS_FAILED.value
    return NotAttemptedReason.TASKS_SKIPPED.value


def _not_attempted_for_covering(
    covering: list[Task],
) -> tuple[NotAttemptedReason, list[TaskReason]]:
    if not covering:
        return NotAttemptedReason.NO_TASK, []
    done = [t for t in covering if t.state == TaskState.DONE]
    if not done:
        return NotAttemptedReason.NO_TASK, _reasons_for(covering)
    # PR 6/8: answered/… from counting claims. Core has no claim pipeline.
    outcomes = [t.outcome.value if t.outcome is not None else None for t in done]
    return NotAttemptedReason(not_attempted_reason(outcomes)), _reasons_for(done)


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
