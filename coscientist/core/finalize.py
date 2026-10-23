"""Study finalizer: StudyFinalized with report or minimal_report (total)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from coscientist.core.digest import canonical_json
from coscientist.core.dispositions import dispositions, report_hypothesis_verdicts
from coscientist.core.domain import Task, TaskOutcome, TaskState
from coscientist.core.errors import CASUnavailable, Conflict, LeaseLost
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.finalize_nodes import conclusion_ids_for
from coscientist.core.minimal_report import build_minimal_report, render_markdown_report
from coscientist.core.reasons import TaskReason, TerminalReason
from coscientist.core.study_view import study_view

if TYPE_CHECKING:
    from coscientist.core.machine import StudyRuntime

log = logging.getLogger(__name__)

FinalizeTrigger = Literal["cancel", "budget", "deadline", "all_done", "intake"]

# Bound rich StudyFinalized payload (JCS); oversized → last-resort.
MAX_FINALIZED_PAYLOAD_BYTES = 65_536

_TRIGGER_TERMINAL: dict[str, str] = {
    "cancel": TerminalReason.CANCELLED.value,
    "budget": TerminalReason.BUDGET.value,
    "deadline": TerminalReason.DEADLINE.value,
    "intake": TerminalReason.REFUSED.value,
    "all_done": TerminalReason.COMPLETED.value,
}
_TASKDONE_DETAIL_LINES = {
    "foreach_quorum_gt_children": "не выполнено: кворум > числа детей",
    "quorum": "не выполнено: кворум входов",
    "foreach_empty": "не выполнено: foreach пуст",
}

def is_finalized(runtime: StudyRuntime) -> bool:
    return bool(getattr(runtime, "_finalized", False))

def pick_finalize_trigger(
    *,
    cancelled: bool = False,
    budget: bool = False,
    refused: bool = False,
    deadline: bool = False,
    all_done: bool = False,
) -> FinalizeTrigger | None:
    """Same-tick tie-break: cancelled > budget > intake > deadline > all_done.

    Intake (``StudyRefused``) beats deadline so a crash past ``ed`` still
    finalizes as refused, not deadline.
    """
    if cancelled:
        return "cancel"
    if budget:
        return "budget"
    if refused:
        return "intake"
    if deadline:
        return "deadline"
    if all_done:
        return "all_done"
    return None

def terminal_reason_for(
    trigger: FinalizeTrigger,
    *,
    tasks: Mapping[str, Task],
    question_dispositions: Mapping[str, Mapping[str, Any]],
) -> TerminalReason:
    """Map finalize trigger → TerminalReason (§0.2)."""
    if trigger == "cancel":
        return TerminalReason.CANCELLED
    if trigger == "budget":
        return TerminalReason.BUDGET
    if trigger == "deadline":
        return TerminalReason.DEADLINE
    if trigger == "intake":
        return TerminalReason.REFUSED
    req = [t for t in tasks.values() if t.spec.required and t.state == TaskState.DONE]
    if any(
        t.outcome == TaskOutcome.FAILED and t.reason == TaskReason.HUMAN_TIMEOUT
        for t in req
    ):
        return TerminalReason.HUMAN_TIMEOUT
    _ = question_dispositions
    return TerminalReason.COMPLETED

def expansion_not_done_line(reason: str, detail: str) -> str:
    """Report line for ExpansionRefused (DESIGN_M1 §4 — Russian wording)."""
    r, d = str(reason or ""), str(detail or "")
    if d.startswith("foreach_"):
        if "assign_keys" in d or "exception" in d:
            return "не выполнено: ошибка foreach"
        return "не выполнено: невалидный запрос"
    if r in ("invalid", "invalid_hitl") or d.startswith("invalid"):
        return "не выполнено: невалидный запрос"
    if r == "has_refutation":
        return "не выполнено: есть опровержение"
    if r == "critic_rejected":
        return "не выполнено: отказ критика"
    return "не выполнено: лимит расширений"  # quota / reserve / expansion_limit

def contributing_reasons_ordered(events: list[EventEnvelope]) -> list[str]:
    """TaskReason values + ExpansionRefused / foreach / quorum report lines."""
    seen: list[str] = []
    for ev in sorted(events, key=lambda e: e.seq or 0):
        if ev.type == "TaskDone":
            reason = ev.payload.get("reason")
            if reason and reason not in seen:
                try:
                    TaskReason(reason)
                except ValueError:
                    continue
                seen.append(str(reason))
            line = _TASKDONE_DETAIL_LINES.get(str(ev.payload.get("detail") or ""))
            if line and line not in seen:
                seen.append(line)
        elif ev.type == "ExpansionRefused":
            line = expansion_not_done_line(
                str(ev.payload.get("reason") or ""),
                str(ev.payload.get("detail") or ""),
            )
            if line not in seen:
                seen.append(line)
        elif ev.type == "PlanExpanded" and ev.payload.get("foreach"):
            _append_foreach_report_lines(seen, ev.payload)
    return seen

def _append_foreach_report_lines(seen: list[str], p: dict[str, Any]) -> None:
    items, added = p.get("foreach_items") or [], p.get("added_tasks") or []
    dropped = [str(x) for x in (p.get("foreach_dropped") or [])]
    total = p.get("foreach_total")
    if total is None:
        total = len(items) + len(dropped)
    refute = "не выполнено: проверка опровержения не выполнена"
    if not items and not added:
        if "не выполнено: foreach пуст" not in seen:
            seen.append("не выполнено: foreach пуст")
        if p.get("foreach_refute") and refute not in seen:
            seen.append(refute)
        return
    line = f"foreach: развёрнуто {len(added)}"
    if line not in seen:
        seen.append(line)
    if p.get("foreach_capped") or dropped:
        cap = f"ограничение foreach: взято {len(added)} из {total}"
        if dropped:
            cap += f", отброшены {', '.join(dropped)}"
        if cap not in seen:
            seen.append(cap)
    if p.get("foreach_refute"):
        for did in dropped:
            r = f"{refute} ({did})"
            if r not in seen:
                seen.append(r)
    for n in p.get("foreach_rank_notes") or []:
        if n and str(n) not in seen:
            seen.append(str(n))

def last_resort_payload(
    *,
    study_id: str,
    trigger: FinalizeTrigger,
    fallback_reason: str,
    refusal: str | None = None,
) -> dict[str, Any]:
    """Constant-only StudyFinalized payload (no tasks/plans/store access).

    I2 deviation: ``question_dispositions`` is empty, so fold emits only the
    study-level conclusion via ``conclusion_ids_for(study_id, {})``. Questions have
    no per-question conclusion (nor ``minimal_report.questions``). Prefer rich/minimal.
    """
    term = _TRIGGER_TERMINAL.get(trigger, TerminalReason.COMPLETED.value)
    minimal: dict[str, Any] = {
        "study_id": study_id,
        "terminal_reason": term,
        "trigger": trigger,
        "contributing_reasons": [],
        "summary": f"Study закрыто: {term}; задач 0 (SUCCESS 0, PARTIAL 0, "
        f"FAILED 0, SKIPPED 0); вопросов 0, not_attempted 0",
        "question_count": 0,
        "not_attempted_count": 0,
        "fallback_reason": fallback_reason[:200],
        "refusal": refusal,
        "verdict": "failed",
        "tasks": [],
        "questions": [],
        "sections": {"real": [], "stub": [], "human": []},
        "truncated": False,
    }
    return {
        "trigger": trigger,
        "terminal_reason": term,
        "contributing_reasons": [],
        "verdict": "failed",
        "question_dispositions": {},
        "report_artifact_id": None,
        "conclusion_ids": conclusion_ids_for(study_id, {}),
        "minimal_report": minimal,
        "fallback_reason": fallback_reason,
        "refusal": refusal,
    }

async def finalize(runtime: StudyRuntime, *, trigger: FinalizeTrigger) -> None:
    """Write StudyFinalized (idempotent). Always attempts a write; LeaseLost propagates.

    Conflict on the StudyFinalized idem key is success only when that event is
    already in the store (commit succeeded, fold/local project failed). Otherwise
    the Conflict is re-raised — never mark finalized without the event.
    """
    if runtime.study is None or is_finalized(runtime):
        return
    sid = runtime.study.study_id
    refusal = _refusal_payload(runtime, trigger)
    try:
        payload = await _build_rich_payload(runtime, sid=sid, trigger=trigger)
    except Exception as e:  # noqa: BLE001 — last-resort totality
        log.warning("finalize rich path failed: %s", e, exc_info=True)
        payload = last_resort_payload(
            study_id=sid,
            trigger=trigger,
            fallback_reason=type(e).__name__,
            refusal=refusal,
        )
    try:
        await _append_finalized(runtime, sid=sid, payload=payload)
    except LeaseLost:
        raise
    except Conflict as err:
        await _sync_finalized_from_store(runtime, sid=sid, conflict=err)
    except Exception as e:  # noqa: BLE001 — retry once with last-resort
        log.warning("finalize append failed: %s; retrying last-resort", e, exc_info=True)
        try:
            await _append_finalized(
                runtime,
                sid=sid,
                payload=last_resort_payload(
                    study_id=sid,
                    trigger=trigger,
                    fallback_reason=type(e).__name__,
                    refusal=refusal,
                ),
            )
        except Conflict as err:
            await _sync_finalized_from_store(runtime, sid=sid, conflict=err)
        except LeaseLost:
            raise
    runtime._finalized = True  # noqa: SLF001

async def _sync_finalized_from_store(
    runtime: StudyRuntime, *, sid: str, conflict: Conflict
) -> None:
    """On Conflict: succeed only if StudyFinalized idem event exists; else re-raise."""
    expected = make_idem("StudyFinalized", sid)
    for ev in await runtime.store.read_all():
        if ev.idem_key == expected:
            if not is_finalized(runtime):
                runtime._project_local(ev)  # noqa: SLF001
            return
    raise conflict

async def _append_finalized(
    runtime: StudyRuntime, *, sid: str, payload: dict[str, Any]
) -> None:
    now = runtime.clock.now()
    await runtime._append(  # noqa: SLF001
        [
            EventEnvelope(
                study_id=sid,
                type="StudyFinalized",
                entity_key=sid,
                idem_key=make_idem("StudyFinalized", sid),
                payload=payload,
                ts=now,
                schema_version=3,
            )
        ]
    )

def _bound_rich_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Shrink question_dispositions / reasons until JCS ≤ MAX; else raise."""
    out = dict(payload)
    if len(canonical_json(out)) <= MAX_FINALIZED_PAYLOAD_BYTES:
        return out
    qdisp = {
        qid: {
            **(d if isinstance(d, dict) else {}),
            "formulation": str((d or {}).get("formulation", ""))[:80]
            if isinstance(d, dict)
            else "",
        }
        for qid, d in (out.get("question_dispositions") or {}).items()
    }
    out["question_dispositions"] = qdisp
    out["contributing_reasons"] = list(out.get("contributing_reasons") or [])[:10]
    if len(canonical_json(out)) <= MAX_FINALIZED_PAYLOAD_BYTES:
        return out
    raise RuntimeError("finalized_payload_too_large")

def _artifact_lookup(runtime: StudyRuntime) -> Any | None:
    """``get_meta`` when an artifact store is attached (live replay); else None."""
    arts = getattr(runtime, "artifacts", None)
    if arts is None:
        ex = getattr(runtime, "executor", None)
        arts = getattr(ex, "artifacts", None) if ex is not None else None
    lookup = getattr(arts, "get_meta", None)
    return lookup if callable(lookup) else None

async def _build_rich_payload(
    runtime: StudyRuntime, *, sid: str, trigger: FinalizeTrigger
) -> dict[str, Any]:
    plans = await _load_plans(runtime)
    lookup = _artifact_lookup(runtime)
    qdisp = dispositions(
        tasks=runtime.tasks,
        plans=plans,
        graph=getattr(runtime, "graph", None),
        study=runtime.study,
        results=getattr(runtime, "results", None),
        artifact_lookup=lookup,
    )
    hyp_verdicts, hyp_unavail = report_hypothesis_verdicts(
        graph=getattr(runtime, "graph", None),
        study=runtime.study,
        results=getattr(runtime, "results", None),
        tasks=runtime.tasks,
        artifact_lookup=lookup,
    )
    term = terminal_reason_for(
        trigger, tasks=runtime.tasks, question_dispositions=qdisp
    )
    events = await runtime.store.read_all()
    contrib = contributing_reasons_ordered(events)
    study = runtime.study
    assert study is not None
    view = study_view(
        runtime.tasks.values(),
        open_hitl=0,
        attempts=runtime.attempts,
        finalized=False,
        now=runtime.clock.now(),
        effective_deadline=study.effective_deadline(),
        question_dispositions=qdisp,
    )
    verdict = view.verdict or "partial"
    payload = await _build_finalized_payload(
        runtime,
        sid=sid,
        trigger=trigger,
        term=term,
        contrib=contrib,
        verdict=verdict,
        qdisp=qdisp,
        hypothesis_verdicts=hyp_verdicts,
        hypotheses_unavailable=hyp_unavail,
    )
    return _bound_rich_payload(payload)

async def _load_plans(runtime: StudyRuntime) -> dict[str, dict[str, Any]]:
    local = getattr(runtime, "plans", None)
    if isinstance(local, dict) and local:
        return local
    rows = await runtime.store.get_projection("plans")
    return {str(r.get("plan_id") or i): r for i, r in enumerate(rows)}

def _refusal_payload(runtime: StudyRuntime, trigger: FinalizeTrigger) -> str | None:
    if trigger != "intake":
        return None
    ref = getattr(runtime, "_intake_refusal", None)
    if ref is None:
        return None
    kind = getattr(ref, "kind", None)
    reason = getattr(ref, "reason", "")
    kv = getattr(kind, "value", kind)
    return f"{kv}:{reason}" if kv else str(reason)

async def _build_finalized_payload(
    runtime: StudyRuntime,
    *,
    sid: str,
    trigger: FinalizeTrigger,
    term: TerminalReason,
    contrib: list[str],
    verdict: str,
    qdisp: dict[str, dict[str, Any]],
    hypothesis_verdicts: dict[str, dict[str, Any]] | None = None,
    hypotheses_unavailable: str | None = None,
) -> dict[str, Any]:
    fallback: str | None = None
    report_artifact_id: str | None = None
    conclusion_ids = conclusion_ids_for(sid, qdisp)
    minimal: dict[str, Any] | None = None
    refusal = _refusal_payload(runtime, trigger)
    try:
        report_artifact_id = await _primary_report_path(
            runtime,
            sid=sid,
            trigger=trigger,
            term=term,
            verdict=verdict,
            contrib=contrib,
            qdisp=qdisp,
            refusal=refusal,
        )
    except Exception as e:  # noqa: BLE001 — primary failure → minimal_report
        fallback = type(e).__name__
        log.warning("finalize primary path failed: %s", e, exc_info=True)
        try:
            minimal = build_minimal_report(
                study_id=sid,
                terminal_reason=term.value,
                trigger=trigger,
                contributing_reasons=contrib,
                verdict=verdict,
                tasks=runtime.tasks,
                attempts=runtime.attempts,
                question_dispositions=qdisp,
                fallback_reason=fallback,
                refusal=refusal,
                hypotheses=hypothesis_verdicts or {},
                hypotheses_unavailable=hypotheses_unavailable,
            )
        except Exception as e2:  # noqa: BLE001
            raise RuntimeError(f"minimal_report:{type(e2).__name__}") from e2
    return {
        "trigger": trigger,
        "terminal_reason": term.value,
        "contributing_reasons": contrib,
        "verdict": verdict,
        "question_dispositions": qdisp,
        "hypothesis_verdicts": hypothesis_verdicts or {},
        "hypotheses_unavailable": hypotheses_unavailable,
        "report_artifact_id": report_artifact_id,
        "conclusion_ids": conclusion_ids,
        "minimal_report": minimal,
        "fallback_reason": fallback,
        "refusal": refusal,
    }

async def _primary_report_path(
    runtime: StudyRuntime,
    *,
    sid: str,
    trigger: str,
    term: TerminalReason,
    verdict: str,
    contrib: list[str],
    qdisp: dict[str, dict[str, Any]],
    refusal: str | None = None,
) -> str:
    md = render_markdown_report(
        study_id=sid,
        terminal_reason=term.value,
        trigger=trigger,
        verdict=verdict,
        contributing_reasons=contrib,
        question_dispositions=qdisp,
        tasks=runtime.tasks,
        refusal=refusal,
    )
    # Surface infra details (sandbox reason + soft-isolation note) in report text.
    results = getattr(runtime, "results", None) or {}
    detail_lines: list[str] = []
    for rid in sorted(results):
        res = results[rid]
        if not isinstance(res, dict):
            continue
        detail = res.get("detail")
        if detail:
            detail_lines.append(f"- result `{rid}` detail: {detail}")
        reason = res.get("reason")
        if reason:
            detail_lines.append(f"- result `{rid}` reason: {reason}")
    if detail_lines:
        md = md + "\n## Infra / result details\n" + "\n".join(detail_lines) + "\n"
    arts = _artifact_store(runtime)
    if arts is None:
        raise CASUnavailable("no artifact store on runtime")
    from coscientist.core.artifacts import make_tool_ref

    art = arts.put(
        md.encode("utf-8"),
        make_tool_ref(f"finalize:{sid}"),
        name="report.md",
        media_type="text/markdown",
    )
    return art.artifact_id

def _artifact_store(runtime: StudyRuntime) -> Any:
    ex = getattr(runtime, "executor", None)
    if ex is not None and getattr(ex, "artifacts", None) is not None:
        return ex.artifacts
    return getattr(runtime, "artifacts", None)
