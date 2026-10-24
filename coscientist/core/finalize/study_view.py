"""Derived study status — never stored (DESIGN §3.2 / §3.4)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from coscientist.core.claims.rules import study_fulfilled
from coscientist.core.execution.domain import StudyView, Task, TaskOutcome, TaskState


def _task_is_stub(task: Task, attempts: Mapping[str, Mapping[str, Any]]) -> bool:
    """True iff the last closed attempt has provenance=stub."""
    for aid in reversed(task.attempts):
        att = attempts.get(aid, {})  # audit-ok[dict_get_default]: explicit default only for structural absent key
        if att.get("closed"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            return att.get("provenance") == "stub"  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
    return False


def _q_verdicts_from_dispositions(
    question_dispositions: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, str]:
    """Disposition/verdict strings for study_fulfilled (same mapping as finalize)."""
    if not question_dispositions:
        return {}
    out: dict[str, str] = {}  # audit-ok[field_default]: structural empty/collector; not invented domain value
    for qid, d in question_dispositions.items():
        if not isinstance(d, dict):
            continue
        out[str(qid)] = str(d.get("disposition") or d.get("verdict") or "untested")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_call]: coalesce existing call result; no invented literal # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # audit-ok[or_call]: coalesce existing call result; no invented literal # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # noqa: E501
    return out


def _compute_verdict(
    tasks: list[Task],
    *,
    atts: Mapping[str, Mapping[str, Any]],
    question_dispositions: Mapping[str, Mapping[str, Any]] | None,
    question_final_types: Mapping[str, str] | None = None,
) -> tuple[str, float]:
    """Task-level verdict, then cap via study_fulfilled (single source with finalize)."""
    done_tasks = [t for t in tasks if t.state == TaskState.DONE]
    done = len(done_tasks)
    stub_share = (sum(1 for t in done_tasks if _task_is_stub(t, atts)) / done) if done else 0.0  # audit-ok[ternary_default]: branch selects typed literal required by contract  # noqa: E501
    # report is required=False (PR 2) — excluded when required=False
    req = [t for t in tasks if t.spec.required]
    req_ok = {TaskOutcome.SUCCESS, TaskOutcome.PARTIAL}
    has_required_stub = any(_task_is_stub(t, atts) for t in req)

    if not req:
        verdict = "partial"
    elif all(t.outcome in {TaskOutcome.FAILED, TaskOutcome.SKIPPED} for t in req):
        verdict = "failed"
    elif any(t.kind.value == "plan" and t.outcome in {TaskOutcome.FAILED, TaskOutcome.SKIPPED} for t in tasks):
        verdict = "failed"
    elif all(t.outcome in req_ok for t in req) and has_required_stub:
        verdict = "stubbed"
    elif all(t.outcome == TaskOutcome.SUCCESS for t in req):
        verdict = "fulfilled"
    else:
        verdict = "partial"

    # PR13: same gate as finalize — study_fulfilled + final_types (never diverge).
    if verdict in ("fulfilled", "stubbed"):
        all_required_success = bool(req) and all(  # audit-ok[coerce_bool]: bool() after explicit type gate
            t.state == TaskState.DONE and t.outcome in req_ok for t in req
        )
        q_verdicts = _q_verdicts_from_dispositions(question_dispositions)
        ftypes = {str(k): str(v) for k, v in (question_final_types or {}).items()}  # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # noqa: E501
        if not study_fulfilled(q_verdicts, all_required_success, question_final_types=ftypes):
            verdict = "partial"
    return verdict, stub_share


def study_phase(*, finalized: bool = False, all_done: bool = False, past_effective: bool = False) -> str:  # audit-ok[param_default]: explicit API/default param; not silent domain invent  # noqa: E501
    """Pure phase: closed | finalizing | running (DESIGN §3.2)."""
    if finalized:
        return "closed"
    if all_done or past_effective:  # audit-ok[or_name]: coalesce existing name/attr; no invented literal
        return "finalizing"
    return "running"


def study_view(
    tasks: Iterable[Task],
    *,
    open_hitl: int = 0,  # audit-ok[param_default]: explicit API/default param; not silent domain invent
    attempts: Mapping[str, Mapping[str, Any]] | None = None,
    finalized: bool = False,  # audit-ok[param_default]: explicit API/default param; not silent domain invent
    now: datetime | None = None,
    effective_deadline: datetime | None = None,
    question_dispositions: Mapping[str, Mapping[str, Any]] | None = None,
    question_final_types: Mapping[str, str] | None = None,
) -> StudyView:
    """Compute StudyView.

    closed ⇔ finalized (∃ StudyFinalized). finalizing when all DONE or past
    effective_deadline without a final event. Fulfilled/stubbed are capped via
    ``study_fulfilled`` + ``question_final_types`` (same source as finalize).
    """
    tasks = list(tasks)
    atts: Mapping[str, Mapping[str, Any]] = attempts or {}  # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # noqa: E501
    total = len(tasks)
    done = sum(1 for t in tasks if t.state == TaskState.DONE)
    all_done = bool(tasks) and done == total  # audit-ok[coerce_bool]: bool() after explicit type gate
    past_effective = now is not None and effective_deadline is not None and now >= effective_deadline
    phase = study_phase(finalized=finalized, all_done=all_done, past_effective=past_effective)
    if phase == "closed":
        if not tasks:
            return StudyView(phase="closed", progress=1.0, awaiting_human=False, stub_share=0.0)
        verdict, stub_share = _compute_verdict(
            tasks,
            atts=atts,
            question_dispositions=question_dispositions,
            question_final_types=question_final_types,
        )
        return StudyView(
            phase="closed",
            progress=1.0,
            awaiting_human=False,
            verdict=verdict,  # type: ignore[arg-type]
            stub_share=stub_share,
        )
    if phase == "finalizing":
        verdict, stub_share = _compute_verdict(
            tasks,
            atts=atts,
            question_dispositions=question_dispositions,
            question_final_types=question_final_types,
        )
        return StudyView(
            phase="finalizing",
            progress=done / total if total else 1.0,  # audit-ok[ternary_default]: branch selects typed literal required by contract  # noqa: E501
            awaiting_human=False,
            verdict=verdict,  # type: ignore[arg-type]
            stub_share=stub_share,
        )
    if not tasks:
        return StudyView(phase="running", progress=0.0, awaiting_human=open_hitl > 0, stub_share=0.0)
    stub_share = 0.0
    done_tasks = [t for t in tasks if t.state == TaskState.DONE]
    if done_tasks:
        stub_share = sum(1 for t in done_tasks if _task_is_stub(t, atts)) / len(done_tasks)
    return StudyView(
        phase="running",
        progress=done / total,
        awaiting_human=open_hitl > 0,
        stub_share=stub_share,
    )
