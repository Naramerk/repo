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

FinalizeTrigger = Literal["cancel", "budget", "deadline", "all_done", "intake", "internal_error"]


def _escape_report_text(value: object) -> str:
    """Escape agent/host detail so it cannot inject markdown/HTML structure.

    Keeps alphanumeric reason codes readable (e.g. ``sandbox_unavailable``) while
    neutralizing fence/link/HTML injection and collapsing newlines.
    """
    text = " ".join(str(value).split())[:2000]
    return (
        text.replace("\\", "\\\\")
        .replace("`", "'")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace("http://", "hxxp://")
        .replace("https://", "hxxps://")
    )


# Bound rich StudyFinalized payload (JCS); oversized → last-resort.
MAX_FINALIZED_PAYLOAD_BYTES = 65_536

_TRIGGER_TERMINAL: dict[str, str] = {
    "cancel": TerminalReason.CANCELLED.value,
    "budget": TerminalReason.BUDGET.value,
    "deadline": TerminalReason.DEADLINE.value,
    "intake": TerminalReason.REFUSED.value,
    "all_done": TerminalReason.COMPLETED.value,
    "internal_error": TerminalReason.INTERNAL_ERROR.value,
}
_TASKDONE_DETAIL_LINES = {
    "foreach_quorum_gt_children": "не выполнено: кворум > числа детей",
    "quorum": "не выполнено: кворум входов",
    "foreach_empty": "не выполнено: foreach пуст",
    "share_exhausted": "не выполнено: бюджет задачи исчерпан",
    "task_share_exhausted": "не выполнено: бюджет задачи исчерпан",
    "zero_share_or_pool": "не выполнено: бюджет задачи исчерпан",
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
    if trigger == "internal_error":
        return TerminalReason.INTERNAL_ERROR
    req = [t for t in tasks.values() if t.spec.required and t.state == TaskState.DONE]
    if any(t.outcome == TaskOutcome.FAILED and t.reason == TaskReason.HUMAN_TIMEOUT for t in req):
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


# Guest sandbox timeout exhaustion (PR17) — Russian report lines.
TIMEOUT_BUDGET_RU = "не проверено в пределах бюджета"
IDLE_TIMEOUT_RU = "не проверено: простой агента (подозрение на саботаж)"
# Must survive `_bound_rich_payload` contributing_reasons truncation.
_PROTECTED_CONTRIBUTING_REASONS = frozenset({TIMEOUT_BUDGET_RU, IDLE_TIMEOUT_RU})


def sandbox_timeout_report_line(reason: str | None, detail: str | None = None) -> str | None:
    """Russian line when a task closes after guest timeouts.

    Match by ``TaskReason`` only — never substring-match ``detail`` (avoids
    false positives on unrelated reasons that mention \"timeout\").
    """
    _ = detail  # kept for call-site compat; classification is reason-only
    r = str(reason or "")
    if r == TaskReason.IDLE_TIMEOUT.value:
        return IDLE_TIMEOUT_RU
    if r == TaskReason.SANDBOX_TIMEOUT.value:
        return TIMEOUT_BUDGET_RU
    return None


def contributing_reasons_ordered(events: list[EventEnvelope]) -> list[str]:
    """TaskReason + ExpansionRefused / foreach / quorum / timeout lines (by seq)."""
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
                line = sandbox_timeout_report_line(
                    str(reason),
                    str(ev.payload.get("detail") or ""),
                )
                if line and line not in seen:
                    seen.append(line)
            # PR12: per-task budget line (study-level stays on BudgetExceeded).
            if reason == TaskReason.BUDGET.value:
                from coscientist.core.shares import TASK_BUDGET_EXHAUSTED_LINE

                if TASK_BUDGET_EXHAUSTED_LINE not in seen:
                    seen.append(TASK_BUDGET_EXHAUSTED_LINE)
            line = _TASKDONE_DETAIL_LINES.get(str(ev.payload.get("detail") or ""))
            if line and line not in seen:
                seen.append(line)
            if ev.payload.get("budget_overrun"):
                from coscientist.core.shares import TASK_BUDGET_OVERRUN_LINE

                if TASK_BUDGET_OVERRUN_LINE not in seen:
                    seen.append(TASK_BUDGET_OVERRUN_LINE)
        elif ev.type == "BudgetExceeded":
            from coscientist.core.shares import BUDGET_EXHAUSTED_LINE

            if BUDGET_EXHAUSTED_LINE not in seen:
                seen.append(BUDGET_EXHAUSTED_LINE)
        elif ev.type == "BudgetSharesRefused":
            from coscientist.core.shares import BUDGET_SHARES_REFUSED_LINE

            if BUDGET_SHARES_REFUSED_LINE not in seen:
                seen.append(BUDGET_SHARES_REFUSED_LINE)
        elif ev.type == "ResultRecorded" and ev.payload.get("budget_overrun"):
            from coscientist.core.shares import TASK_BUDGET_OVERRUN_LINE

            if TASK_BUDGET_OVERRUN_LINE not in seen:
                seen.append(TASK_BUDGET_OVERRUN_LINE)
        elif ev.type == "ExpansionRefused":
            custom = (ev.payload or {}).get("report_line")
            if custom:
                line = str(custom)
            else:
                line = expansion_not_done_line(
                    str(ev.payload.get("reason") or ""),
                    str(ev.payload.get("detail") or ""),
                )
            if line not in seen:
                seen.append(line)
        elif ev.type == "PlanExpanded" and ev.payload.get("foreach"):
            _append_foreach_report_lines(seen, ev.payload)
        elif ev.type == "LoopCapped":
            _append_loop_capped_report_line(seen, ev.payload)
        elif ev.type == "LoopRefused":
            _append_loop_refused_report_line(seen, ev.payload)
        elif ev.type == "LoopCompleted":
            _append_loop_completed_report_line(seen, ev.payload)
        elif ev.type == "SideEffectDenied":
            tool = str(ev.payload.get("tool") or "?")
            reason = str(ev.payload.get("reason") or "")
            if reason == "taint_deny":
                line = f"отклонено: tainted контекст ({tool})"
            else:
                line = f"отклонено: побочный эффект ({tool})"
            if line not in seen:
                seen.append(line)
        elif ev.type == "FetchIntegrityRaised":
            if "internal_error" not in seen:
                seen.append("internal_error")
        elif ev.type == "EffectIntended":
            # Track for unknown (Intended without Done) pass below.
            pass
    # PR18: EffectIntended without EffectDone → status unknown in the report.
    intended_keys: set[str] = set()
    done_keys: set[str] = set()
    intended_meta: dict[str, dict[str, Any]] = {}
    for ev in sorted(events, key=lambda e: e.seq or 0):
        if ev.type == "EffectIntended":
            k = str(ev.payload.get("key") or ev.entity_key)
            intended_keys.add(k)
            intended_meta[k] = dict(ev.payload)
        elif ev.type == "EffectDone":
            done_keys.add(str(ev.payload.get("key") or ev.entity_key))
    for k in sorted(intended_keys - done_keys):
        meta = intended_meta.get(k) or {}
        tool = str(meta.get("tool") or "?")
        line = f"эффект неизвестен: {tool}"
        if line not in seen:
            seen.append(line)
    return seen

def _append_loop_capped_report_line(seen: list[str], p: dict[str, Any]) -> None:
    """DESIGN_M1 §4: iteration_cap vs refuse/error (never disguise bugs as cap)."""
    try:
        i = int(p.get("iter") or 0)
    except (TypeError, ValueError):
        i = 0
    try:
        raw_m = p.get("survivors")
        m = int(raw_m) if raw_m is not None else 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        m = 0
    reason = str(p.get("reason") or "iteration_cap")
    detail = str(p.get("detail") or p.get("refuse_detail") or "")
    if reason in ("error", "refused") or detail.startswith("loop_predicate:"):
        line = f"цикл остановлен ошибкой после {i} итераций: {detail or reason}"
        if line not in seen:
            seen.append(line)
        if reason not in seen:
            seen.append(reason)
        return
    line = f"цикл остановлен лимитом после {i} итераций, осталось {m}"
    if line not in seen:
        seen.append(line)
    if "iteration_cap" not in seen:
        seen.append("iteration_cap")


def _append_loop_refused_report_line(seen: list[str], p: dict[str, Any]) -> None:
    """LoopRefused: honest error/refuse line (not iteration_cap)."""
    try:
        i = int(p.get("iter") or 0)
    except (TypeError, ValueError):
        i = 0
    reason = str(p.get("reason") or "error")
    detail = str(p.get("detail") or "")
    line = f"цикл остановлен ошибкой после {i} итераций: {detail or reason}"
    if line not in seen:
        seen.append(line)
    if reason not in seen:
        seen.append(reason)


def _append_loop_completed_report_line(seen: list[str], p: dict[str, Any]) -> None:
    """Until-met completion line (journaled LoopCompleted)."""
    try:
        i = int(p.get("iter") or 0)
    except (TypeError, ValueError):
        i = 0
    try:
        raw_m = p.get("survivors")
        m = int(raw_m) if raw_m is not None else 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        m = 0
    line = f"цикл завершён после {i} итераций, осталось {m}"
    if line not in seen:
        seen.append(line)


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
    except LeaseLost:
        raise
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
        except LeaseLost:
            raise
        except Conflict as err:
            await _sync_finalized_from_store(runtime, sid=sid, conflict=err)
    runtime._finalized = True  # noqa: SLF001


async def _sync_finalized_from_store(runtime: StudyRuntime, *, sid: str, conflict: Conflict) -> None:
    """On Conflict: succeed only if StudyFinalized idem event exists; else re-raise."""
    expected = make_idem("StudyFinalized", sid)
    for ev in await runtime.store.read_all():
        if ev.idem_key == expected:
            if not is_finalized(runtime):
                runtime._project_local(ev)  # noqa: SLF001
            return
    raise conflict


async def _append_finalized(runtime: StudyRuntime, *, sid: str, payload: dict[str, Any]) -> None:
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


def truncate_contributing_reasons(reasons: list[str] | None, *, limit: int = 10) -> list[str]:
    """Truncate contributing_reasons while keeping timeout RU lines.

    ``TIMEOUT_BUDGET_RU`` / ``IDLE_TIMEOUT_RU`` are non-truncatable: they are
    always retained (deduped, original relative order). Other reasons fill the
    remaining slots in first-seen order. If only protected lines remain and
    somehow exceed ``limit``, collapse duplicates into a single counter line.
    """
    src = [str(r) for r in (reasons or [])]
    if len(src) <= limit:
        return src
    pinned: list[str] = []
    pinned_seen: set[str] = set()
    others: list[str] = []
    for r in src:
        if r in _PROTECTED_CONTRIBUTING_REASONS:
            if r not in pinned_seen:
                pinned.append(r)
                pinned_seen.add(r)
        else:
            others.append(r)
    if len(pinned) > limit:
        # Aggregate rather than drop (should not happen with two constants).
        return [f"{pinned[0]} ×{len(pinned)}"][:limit]
    room = max(0, limit - len(pinned))
    return pinned + others[:room]


def _bound_rich_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Shrink question_dispositions / reasons until JCS ≤ MAX; else raise."""
    out = dict(payload)
    if len(canonical_json(out)) <= MAX_FINALIZED_PAYLOAD_BYTES:
        return out
    qdisp = {
        qid: {
            **(d if isinstance(d, dict) else {}),
            "formulation": str((d or {}).get("formulation", ""))[:80] if isinstance(d, dict) else "",
        }
        for qid, d in (out.get("question_dispositions") or {}).items()
    }
    out["question_dispositions"] = qdisp
    out["contributing_reasons"] = truncate_contributing_reasons(list(out.get("contributing_reasons") or []))
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
    # Reclassified fetch tools: journal diagnostic so contrib includes internal_error;
    # paired SourceFetched stays uncounted (fold catches FetchToolReclassified).
    from coscientist.core.source_fetch import reconcile_fetch_integrity

    try:
        await reconcile_fetch_integrity(runtime.writer)
    except Exception:  # noqa: BLE001 — finalize must remain total
        log.warning("reconcile_fetch_integrity failed", exc_info=True)

    plans = await _load_plans(runtime)
    lookup = _artifact_lookup(runtime)
    from coscientist.core.loop import capped_scope_questions

    qdisp = dispositions(
        tasks=runtime.tasks,
        plans=plans,
        graph=getattr(runtime, "graph", None),
        study=runtime.study,
        results=getattr(runtime, "results", None),
        artifact_lookup=lookup,
        loop_capped_questions=capped_scope_questions(runtime),
    )
    hyp_verdicts, hyp_unavail = report_hypothesis_verdicts(
        graph=getattr(runtime, "graph", None),
        study=runtime.study,
        results=getattr(runtime, "results", None),
        tasks=runtime.tasks,
        artifact_lookup=lookup,
    )
    term = terminal_reason_for(trigger, tasks=runtime.tasks, question_dispositions=qdisp)
    events = await runtime.store.read_all()
    contrib = contributing_reasons_ordered(events)
    # PR13: disposition report_line (e.g. «не выполнено: рубрика не принята»).
    for d in qdisp.values():
        line = d.get("report_line") if isinstance(d, dict) else None
        if line and line not in contrib:
            contrib.append(str(line))
    # PlanRefused / invalid final_type lines folded from events.
    for ev in events:
        if ev.type == "PlanRefused":
            line = (ev.payload or {}).get("report_line") or ("не выполнено: план отклонён")
            if line not in contrib:
                contrib.append(str(line))
        if ev.type == "JudgeCasCorrupt":
            line = (ev.payload or {}).get("report_line") or ("не выполнено: внутренняя ошибка (judge CAS)")
            if line not in contrib:
                contrib.append(str(line))
    # Upcaster-coerced unknown final_type → report line on plan questions.
    for plan in plans or []:
        qs = plan.get("questions") if isinstance(plan, dict) else getattr(plan, "questions", None)
        for q in qs or []:
            if isinstance(q, dict):
                line = q.get("final_type_report_line")
                if line and line not in contrib:
                    contrib.append(str(line))
    study = runtime.study
    assert study is not None
    # PR13 §6.1: final_types into study_view; view uses study_fulfilled (single source).
    from coscientist.core.rubric import question_final_types

    ftypes = question_final_types(plans)
    view = study_view(
        runtime.tasks.values(),
        open_hitl=0,
        attempts=runtime.attempts,
        finalized=False,
        now=runtime.clock.now(),
        effective_deadline=study.effective_deadline(),
        question_dispositions=qdisp,
        question_final_types=dict(ftypes),
    )
    # Never raise above study_view — it already applied study_fulfilled.
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
    # Escape agent/host-provided strings so markdown/HTML injection cannot reshape
    # the report structure.
    results = getattr(runtime, "results", None) or {}
    detail_lines: list[str] = []
    for rid in sorted(results):
        res = results[rid]
        if not isinstance(res, dict):
            continue
        detail = res.get("detail")
        if detail:
            detail_lines.append(f"- result `{rid}` detail: {_escape_report_text(detail)}")
        reason = res.get("reason")
        if reason:
            detail_lines.append(f"- result `{rid}` reason: {_escape_report_text(reason)}")
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
