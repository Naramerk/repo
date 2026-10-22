"""Idempotent recover(study_id) (DESIGN §3.5r)."""

from __future__ import annotations

from typing import Any

from coscientist.core.domain import TaskOutcome, TaskState
from coscientist.core.machine import StudyRuntime


async def recover(runtime: StudyRuntime) -> dict[str, Any]:
    """
    - attempt with route_returned and no result → record from saved output (stub in M0)
    - open attempt without reply → AttemptClosed(crashed, retryable) then P3/P4
    - expired deadlines fire on first tick (HITL via recover_after_idle)
    """
    closed = 0
    retried = 0
    now = runtime.clock.now()
    await runtime.recover_after_idle()

    for aid, att in list(runtime.attempts.items()):
        if att.get("closed"):
            continue
        task = runtime.tasks.get(att["task_id"])
        if task is None:
            continue
        if att.get("route_returned") and att.get("agent_output_digest"):
            await runtime.finish_attempt(aid, outcome=TaskOutcome.SUCCESS)
            closed += 1
            continue
        await runtime.fail_attempt(aid, retryable=True)
        closed += 1
        task_after = runtime.tasks[att["task_id"]]
        if (
            task_after.state != TaskState.DONE
            and int(att["n"]) < runtime.max_attempts
            and now < task.deadline
        ):
            try:
                await runtime.start_attempt(
                    task.task_id,
                    provenance=str(att.get("provenance", "real")),
                )
                retried += 1
            except Exception:
                pass

    await runtime.tick()
    return {"closed": closed, "retried": retried}
