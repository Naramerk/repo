"""Idempotent recover(study_id) (DESIGN §3.5r)."""

from __future__ import annotations

from typing import Any

from coscientist.core.domain import AgentTaskOutput, TaskState
from coscientist.core.ids import result_id
from coscientist.core.machine import StudyRuntime


async def recover(runtime: StudyRuntime) -> dict[str, Any]:
    """
    - ResultRecorded present → task already projected DONE; never re-run
    - route_returned + CAS digest, no result → Executor.record from saved output
    - open attempt without saved output → AttemptClosed(crashed, retryable) then P3
    """
    closed = 0
    retried = 0
    await runtime.recover_after_idle()

    for aid, att in list(runtime.attempts.items()):
        if att.get("closed"):
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

        digest = att.get("agent_output_digest")
        if att.get("route_returned") and digest:
            if runtime.executor is None:
                raise RuntimeError("recover requires runtime.executor for record path")
            out = _load_agent_output(runtime, digest)
            if out is None:
                await runtime.fail_attempt(aid, retryable=True, close_reason="crashed")
                closed += 1
                if _retry_scheduled(runtime, att["task_id"]):
                    retried += 1
                continue
            await runtime.executor.record(aid, out)
            closed += 1
            continue

        await runtime.fail_attempt(aid, retryable=True, close_reason="crashed")
        closed += 1
        if _retry_scheduled(runtime, att["task_id"]):
            retried += 1

    await runtime.tick()
    return {"closed": closed, "retried": retried}


def _retry_scheduled(runtime: StudyRuntime, task_id: str) -> bool:
    task = runtime.tasks.get(task_id)
    return (
        task is not None
        and task.state == TaskState.PENDING
        and task.reason == "retry_scheduled"
    )


def _load_agent_output(runtime: StudyRuntime, digest: str) -> AgentTaskOutput | None:
    arts = getattr(runtime.executor, "artifacts", None)
    if arts is None:
        return None
    try:
        raw = arts.read_cas(digest)
    except (FileNotFoundError, OSError):
        return None
    return AgentTaskOutput.model_validate_json(raw)
