"""Study-level cancel → StudyCancelled + SKIPPED(cancelled) + finalize(cancel)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coscientist.core.claims.reasons import CloseReason, TaskReason
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.execution.domain import TaskOutcome, TaskState
from coscientist.core.finalize import finalize as finalize_mod
from coscientist.core.events.schema_versions import (
    ATTEMPT_CLOSED_SCHEMA_VERSION,
    STUDY_CANCELLED_SCHEMA_VERSION,
    TASK_DONE_SCHEMA_VERSION,
)

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime

async def cancel_study(runtime: StudyRuntime, by: str = "user") -> None:
    """Emit StudyCancelled, close open work as cancelled, then finalize(trigger=cancel)."""
    if runtime.study is None or finalize_mod.is_finalized(runtime):
        return
    sid = runtime.study.study_id
    now = runtime.clock.now()
    events: list[EventEnvelope] = [
        EventEnvelope(
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
) -> list[EventEnvelope]:
    assert runtime.study is not None
    sid = runtime.study.study_id
    now = runtime.clock.now()
    events: list[EventEnvelope] = []
    for task in list(runtime.tasks.values()):
        if task.state == TaskState.DONE:
            continue
        for aid in task.attempts:
            if not runtime.attempts.get(aid, {}).get("closed"):
                events.append(
                    EventEnvelope(
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
                t = runtime._attempt_tasks.pop(aid, None)  # noqa: SLF001
                if t:
                    t.cancel()
        events.append(
            EventEnvelope(
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
