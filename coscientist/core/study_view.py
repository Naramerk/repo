"""Derived study status — never stored (DESIGN §3.2)."""

from __future__ import annotations

from collections.abc import Iterable

from coscientist.core.domain import StudyView, Task, TaskOutcome, TaskState


def study_view(
    tasks: Iterable[Task],
    *,
    open_hitl: int = 0,
    simulated_done: int | None = None,
) -> StudyView:
    tasks = list(tasks)
    if not tasks:
        return StudyView(phase="running", progress=0.0, awaiting_human=open_hitl > 0)

    total = len(tasks)
    done = sum(1 for t in tasks if t.state == TaskState.DONE)
    if any(t.state != TaskState.DONE for t in tasks):
        return StudyView(
            phase="running",
            progress=done / total,
            awaiting_human=open_hitl > 0,
        )

    req = [t for t in tasks if t.spec.required]
    stub_share = 0.0
    if simulated_done is not None and done:
        stub_share = simulated_done / done

    if not req:
        verdict: str = "partial"
    elif all(t.outcome == TaskOutcome.SUCCESS for t in req):
        verdict = "fulfilled"
    elif all(t.outcome in {TaskOutcome.FAILED, TaskOutcome.SKIPPED} for t in req):
        verdict = "failed"
    elif any(
        t.kind.value == "plan" and t.outcome in {TaskOutcome.FAILED, TaskOutcome.SKIPPED}
        for t in tasks
    ):
        verdict = "failed"
    elif stub_share > 0 and all(
        t.outcome in {TaskOutcome.SUCCESS, TaskOutcome.PARTIAL} for t in req
    ):
        verdict = "simulated"
    else:
        verdict = "partial"

    return StudyView(
        phase="closed",
        progress=1.0,
        awaiting_human=False,
        verdict=verdict,  # type: ignore[arg-type]
        stub_share=stub_share,
    )
