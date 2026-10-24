"""Study-level cancel → StudyCancelled + SKIPPED(cancelled) + finalize(cancel)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coscientist.core.claims.reasons import CloseReason, TaskReason
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.mapping import optional_item
from coscientist.core.events.schema_versions import (
    ATTEMPT_CLOSED_SCHEMA_VERSION,
    STUDY_CANCELLED_SCHEMA_VERSION,
    TASK_DONE_SCHEMA_VERSION,
)
from coscientist.core.execution.domain import TaskOutcome, TaskState
from coscientist.core.finalize import finalize as finalize_mod

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime


async def cancel_study(runtime: StudyRuntime, by: str) -> None:
    """Emit StudyCancelled, close open work as cancelled, then finalize(trigger=cancel)."""
    if any((not isinstance(by, str), not by)):
        raise ValueError("cancel_study requires non-empty by=")
    if any((runtime.study is None, finalize_mod.is_finalized(runtime))):
        return
    sid = runtime.study.study_id
    now = runtime.clock.now()
    events: list[UnstampedEvent] = [
        UnstampedEvent(
            study_id=sid,
            type="StudyCancelled",
            entity_key=sid,
            idem_key=make_idem("StudyCancelled", sid),
            payload={"study_id": sid, "by": by, "at": now.isoformat()},
            ts=now,
            schema_version=STUDY_CANCELLED_SCHEMA_VERSION,
        )
    ]
    events.extend(
        _close_open_work(
            runtime, reason=TaskReason.CANCELLED, close=CloseReason.CANCELLED
        )
    )
    await runtime._append(events)  # noqa: SLF001
    runtime._study_cancelled = True  # noqa: SLF001
    # Module attribute — tests patch ``finalize_mod.finalize`` reliably.
    await finalize_mod.finalize(runtime, trigger="cancel")


def _close_open_work(
    runtime: Any,
    *,
    reason: TaskReason,
    close: CloseReason,
) -> list[UnstampedEvent]:
    if runtime.study is None:
        raise RuntimeError("cancel_study close_open_work requires runtime.study")
    sid = runtime.study.study_id
    now = runtime.clock.now()
    events = []
    for task in list(runtime.tasks.values()):
        if task.state == TaskState.DONE:
            continue
        for aid in task.attempts:
            attempt = optional_item(runtime.attempts, aid)
            if attempt is None:
                raise JournalFormatError(
                    f"attempt {aid!r} missing from runtime.attempts"
                )
            if "closed" not in attempt:
                raise JournalFormatError(
                    f"attempt {aid!r} missing required closed field"
                )
            closed = attempt["closed"]
            if not isinstance(closed, bool):
                raise JournalFormatError(
                    f"attempt {aid!r} closed must be bool, got {type(closed).__name__}"
                )
            if closed:
                continue
            events.append(
                UnstampedEvent(
                    study_id=sid,
                    type="AttemptClosed",
                    entity_key=aid,
                    idem_key=make_idem("AttemptClosed", aid, close.value),
                    payload={
                        "attempt_id": aid,
                        "closed": True,
                        "close_reason": close.value,
                        "task_id": task.task_id,
                    },
                    ts=now,
                    schema_version=ATTEMPT_CLOSED_SCHEMA_VERSION,
                )
            )
            if aid in runtime._attempt_tasks:
                t = runtime._attempt_tasks.pop(aid)
            else:
                t = None  # noqa: SLF001
            if t:
                t.cancel()
        events.append(
            UnstampedEvent(
                study_id=sid,
                type="TaskDone",
                entity_key=task.task_id,
                idem_key=make_idem("TaskDone", task.task_id),
                payload={
                    "task_id": task.task_id,
                    "outcome": TaskOutcome.SKIPPED.value,
                    "reason": reason.value,
                },
                ts=now,
                schema_version=TASK_DONE_SCHEMA_VERSION,
            )
        )
    return events
