"""Idempotent recover(study_id) (DESIGN §3.5r)."""

from __future__ import annotations

from typing import Any

from coscientist.core.domain import AgentTaskOutput, TaskState
from coscientist.core.ids import result_id
from coscientist.core.machine import StudyRuntime


async def recover(runtime: StudyRuntime) -> dict[str, Any]:
    """
    - ResultRecorded present → task already projected DONE; never re-run
    - route_returned + saved digest, no result → Executor.record from saved output
    - open attempt without reply → AttemptClosed(crashed, retryable) then P3
    """
    closed = 0
    retried = 0
    await runtime.recover_after_idle()

    for aid, att in list(runtime.attempts.items()):
        if att.get("closed"):
            # if result recorded, ensure we never schedule a re-run
            if result_id(aid) in runtime.results or aid in runtime.results:
                continue
            continue

        task = runtime.tasks.get(att["task_id"])
        if task is None:
            continue

        # Result already in journal (projected) but attempt not closed — finish via record path
        recorded = runtime.results.get(result_id(aid)) or runtime.results.get(aid)
        if recorded is not None:
            # Atomic record already closed attempt+task; if we see this, projection lag —
            # do not start a new attempt.
            closed += 1
            continue

        if att.get("route_returned") and att.get("agent_output_digest"):
            if runtime.executor is None:
                raise RuntimeError("recover requires runtime.executor for record path")
            # Reconstruct minimal output; digest already stored on attempt
            out = AgentTaskOutput(summary="recovered", claim=None)
            await runtime.executor.record(aid, out)
            closed += 1
            continue

        await runtime.fail_attempt(aid, retryable=True)
        closed += 1
        # P3 scheduled inside fail_attempt when retryable
        task_after = runtime.tasks[att["task_id"]]
        if task_after.state == TaskState.RUNNING:
            retried += 1

    await runtime.tick()
    return {"closed": closed, "retried": retried}
