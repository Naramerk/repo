"""Derived study status — never stored (DESIGN §3.2 / §3.4)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from coscientist.core.domain import StudyView, Task, TaskOutcome, TaskState


def _task_is_stub(task: Task, attempts: Mapping[str, Mapping[str, Any]]) -> bool:
    """True iff the last closed attempt has provenance=stub."""
    for aid in reversed(task.attempts):
        att = attempts.get(aid, {})
        if att.get("closed"):
            return att.get("provenance") == "stub"
    return False


def study_view(
    tasks: Iterable[Task],
    *,
    open_hitl: int = 0,
    attempts: Mapping[str, Mapping[str, Any]] | None = None,
) -> StudyView:
    """Compute StudyView from tasks (+ optional attempt map for stub provenance).

    Verdict rule (DESIGN §3.4): stubbed ⇔ all required ∈ {SUCCESS, PARTIAL}
    and ≥1 required task has provenance=stub. Non-required stubs only affect
    stub_share. Questions are not considered until PR 2.
    """
    tasks = list(tasks)
    atts: Mapping[str, Mapping[str, Any]] = attempts or {}
    if not tasks:
        return StudyView(phase="running", progress=0.0, awaiting_human=open_hitl > 0)

    total = len(tasks)
    done_tasks = [t for t in tasks if t.state == TaskState.DONE]
    done = len(done_tasks)
    if any(t.state != TaskState.DONE for t in tasks):
        stub_share = (sum(1 for t in done_tasks if _task_is_stub(t, atts)) / done) if done else 0.0
        return StudyView(
            phase="running",
            progress=done / total,
            awaiting_human=open_hitl > 0,
            stub_share=stub_share,
        )

    req = [t for t in tasks if t.spec.required]
    stub_share = (sum(1 for t in done_tasks if _task_is_stub(t, atts)) / done) if done else 0.0
    req_ok = {TaskOutcome.SUCCESS, TaskOutcome.PARTIAL}
    has_required_stub = any(_task_is_stub(t, atts) for t in req)

    if not req:
        verdict: str = "partial"
    elif all(t.outcome in {TaskOutcome.FAILED, TaskOutcome.SKIPPED} for t in req):
        verdict = "failed"
    elif any(
        t.kind.value == "plan" and t.outcome in {TaskOutcome.FAILED, TaskOutcome.SKIPPED}
        for t in tasks
    ):
        verdict = "failed"
    elif all(t.outcome in req_ok for t in req) and has_required_stub:
        verdict = "stubbed"
    elif all(t.outcome == TaskOutcome.SUCCESS for t in req):
        verdict = "fulfilled"
    else:
        verdict = "partial"

    return StudyView(
        phase="closed",
        progress=1.0,
        awaiting_human=False,
        verdict=verdict,  # type: ignore[arg-type]
        stub_share=stub_share,
    )
