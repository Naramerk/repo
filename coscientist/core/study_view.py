"""Derived study status — never stored (DESIGN §3.2 / §3.4)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from coscientist.core.domain import StudyView, Task, TaskOutcome, TaskState


def _task_is_stub(task: Task, attempts: Mapping[str, Mapping[str, Any]]) -> bool:
    """True iff the last closed attempt has provenance=stub."""
    for aid in reversed(task.attempts):
        att = attempts.get(aid, {})
        if att.get("closed"):
            return att.get("provenance") == "stub"
    return False


def _compute_verdict(
    tasks: list[Task],
    *,
    atts: Mapping[str, Mapping[str, Any]],
    question_dispositions: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[str, float]:
    done_tasks = [t for t in tasks if t.state == TaskState.DONE]
    done = len(done_tasks)
    stub_share = (
        (sum(1 for t in done_tasks if _task_is_stub(t, atts)) / done) if done else 0.0
    )
    # report is required=False (PR 2) — excluded when required=False
    req = [t for t in tasks if t.spec.required]
    req_ok = {TaskOutcome.SUCCESS, TaskOutcome.PARTIAL}
    has_required_stub = any(_task_is_stub(t, atts) for t in req)

    if not req:
        verdict = "partial"
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

    # PR 2: ≥1 not_attempted → verdict not above partial
    if question_dispositions and any(
        d.get("disposition") == "not_attempted" for d in question_dispositions.values()
    ):
        if verdict == "fulfilled":
            verdict = "partial"
        elif verdict == "stubbed":
            verdict = "partial"
    return verdict, stub_share


def study_phase(
    *, finalized: bool = False, all_done: bool = False, past_effective: bool = False
) -> str:
    """Pure phase: closed | finalizing | running (DESIGN §3.2)."""
    if finalized:
        return "closed"
    if all_done or past_effective:
        return "finalizing"
    return "running"


def study_view(
    tasks: Iterable[Task],
    *,
    open_hitl: int = 0,
    attempts: Mapping[str, Mapping[str, Any]] | None = None,
    finalized: bool = False,
    now: datetime | None = None,
    effective_deadline: datetime | None = None,
    question_dispositions: Mapping[str, Mapping[str, Any]] | None = None,
) -> StudyView:
    """Compute StudyView.

    closed ⇔ finalized (∃ StudyFinalized). finalizing when all DONE or past
    effective_deadline without a final event. Questions cap fulfilled/stubbed
    at partial when any disposition is not_attempted (PR 2).
    """
    tasks = list(tasks)
    atts: Mapping[str, Mapping[str, Any]] = attempts or {}
    if finalized:
        if not tasks:
            return StudyView(phase="closed", progress=1.0, awaiting_human=False)
        verdict, stub_share = _compute_verdict(
            tasks, atts=atts, question_dispositions=question_dispositions
        )
        return StudyView(
            phase="closed",
            progress=1.0,
            awaiting_human=False,
            verdict=verdict,  # type: ignore[arg-type]
            stub_share=stub_share,
        )

    if not tasks:
        return StudyView(phase="running", progress=0.0, awaiting_human=open_hitl > 0)

    total = len(tasks)
    done = sum(1 for t in tasks if t.state == TaskState.DONE)
    all_done = done == total
    past_effective = (
        now is not None
        and effective_deadline is not None
        and now >= effective_deadline
    )
    if all_done or past_effective:
        verdict, stub_share = _compute_verdict(
            tasks, atts=atts, question_dispositions=question_dispositions
        )
        return StudyView(
            phase="finalizing",
            progress=done / total if total else 1.0,
            awaiting_human=False,
            verdict=verdict,  # type: ignore[arg-type]
            stub_share=stub_share,
        )

    stub_share = 0.0
    done_tasks = [t for t in tasks if t.state == TaskState.DONE]
    if done_tasks:
        stub_share = sum(1 for t in done_tasks if _task_is_stub(t, atts)) / len(
            done_tasks
        )
    return StudyView(
        phase="running",
        progress=done / total,
        awaiting_human=open_hitl > 0,
        stub_share=stub_share,
    )
