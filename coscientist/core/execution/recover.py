"""Idempotent recover(study_id) (DESIGN §3.5r)."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from coscientist.core.claims.reasons import TaskReason
from coscientist.core.events.attempt_fields import attempt_closed
from coscientist.core.events.errors import ArtifactCorrupt, LeaseLost
from coscientist.core.events.ids import result_id
from coscientist.core.execution.domain import AgentTaskOutput, TaskState
from coscientist.core.execution.hitl_guard import reject_open_hitl_if_unsupported
from coscientist.core.execution.machine import StudyRuntime


async def recover(runtime: StudyRuntime) -> dict[str, Any]:
    """
    - ResultRecorded present → task already projected DONE; never re-run
    - route_returned + CAS digest, no result → Executor.record from saved output
    - open attempt without saved output → AttemptClosed(crashed, retryable) then P3
    - core + open HITL gates → HitlUnsupported (experimental handles HITL idle grace)
    """
    closed = 0
    retried = 0
    recover_idle = getattr(runtime, "recover_after_idle", None)
    if recover_idle is not None:
        await recover_idle()
    else:
        # Core has no HITL: refuse journals that still have open gates.
        reject_open_hitl_if_unsupported(runtime, await runtime.store.read_all())

    for aid, att in list(runtime.attempts.items()):
        if attempt_closed(att):
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

    # Crash between TaskDone and expand/foreach/loop: re-queue PLAN-n / materialize.
    from coscientist.core.finalize.finalize import FINALIZE_SOFT_FAILURES
    from coscientist.core.plan.expand_trigger import resume_pending_expansions
    from coscientist.core.plan.foreach import resume_pending_foreach
    from coscientist.core.plan.loop import finalize_loop_internal_error, resume_pending_loops

    foreach_n = await resume_pending_foreach(runtime)
    try:
        loop_n = await resume_pending_loops(runtime)
    except LeaseLost:
        raise
    except FINALIZE_SOFT_FAILURES as e:
        await finalize_loop_internal_error(runtime, exc=e, loop=None)
        loop_n = 0
    resumed = await resume_pending_expansions(runtime)
    if not getattr(runtime, "_finalized", False):
        await runtime.tick()
    return {
        "closed": closed,
        "retried": retried,
        "expansions_resumed": resumed,
        "foreach_resumed": foreach_n,
        "loops_resumed": loop_n,
    }


def _retry_scheduled(runtime: StudyRuntime, task_id: str) -> bool:
    task = runtime.tasks.get(task_id)
    return (
        task is not None
        and task.state == TaskState.PENDING
        and task.reason == TaskReason.RETRY_SCHEDULED
    )


def _load_agent_output(runtime: StudyRuntime, digest: str) -> AgentTaskOutput | None:
    arts = getattr(runtime.executor, "artifacts", None)
    if arts is None:
        return None
    try:
        raw = arts.read_cas(digest)
        return AgentTaskOutput.model_validate_json(raw)
    except (FileNotFoundError, OSError, ArtifactCorrupt, ValidationError):
        return None
