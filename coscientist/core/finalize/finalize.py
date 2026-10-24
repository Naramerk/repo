"""Study finalizer: StudyFinalized with report or minimal_report (total)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from coscientist.core.claims.dispositions import dispositions, report_hypothesis_verdicts
from coscientist.core.claims.reasons import TaskReason, TerminalReason
from coscientist.core.events.digest import canonical_json
from coscientist.core.events.errors import (
    CASUnavailable,
    Conflict,
    JournalFormatError,
    LeaseLost,
    StudyClosedError,
)
from coscientist.core.events.events import StoredEvent, UnstampedEvent, make_idem
from coscientist.core.events.schema_versions import (
    STUDY_FINALIZED_SCHEMA_VERSION,
)
from coscientist.core.events.upcasters import require_exact_int
from coscientist.core.execution.domain import Task, TaskOutcome, TaskState
from coscientist.core.finalize.finalize_nodes import conclusion_ids_for
from coscientist.core.finalize.minimal_report import build_minimal_report, render_markdown_report
from coscientist.core.finalize.study_view import study_view


from coscientist.core.events.mapping import optional_item
def _or_empty_str(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    return value


def _dict_or_none(v):
    if isinstance(v, dict):
        return v
    return None



def _or_zero_int(value: object) -> int:
    if value is None:
        return 0
    if any((type(value) is bool, not isinstance(value, int))):
        raise TypeError(f"expected int|None, got {type(value).__name__}")
    return value


CONTRIB_REASONS_LIMIT = 10
_UNKNOWN_TOOL = "?"


def _or_empty_list(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"expected list|tuple|None, got {type(value).__name__}")


def _or_empty_dict(value: object) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise TypeError(f"expected dict|None, got {type(value).__name__}")


if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime

log = logging.getLogger(__name__)

FinalizeTrigger = Literal["cancel", "budget", "deadline", "all_done", "intake", "internal_error"]

def _escape_report_text(value: object) -> str:
    """Escape agent/host detail so it cannot inject markdown/HTML structure.

    Keeps alphanumeric reason codes readable (e.g. ``sandbox_unavailable``) while
    neutralizing fence/link/HTML injection and collapsing newlines.
    """
    text = " ".join(str(value).split())[:2000]  # audit-ok[str_split]: finalize.py L89 str_split structural path not inventing domain value
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
    return runtime._finalized is True

def pick_finalize_trigger(
    *,
    cancelled: bool,
    budget: bool,
    refused: bool,
    deadline: bool,
    all_done: bool,
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
    """Report line for ExpansionRefused (DESIGN_M1 §4 — Russian wording).

    Maps exact ``reason`` tokens (and a closed set of foreach detail codes) —
    never substring classification of free-form detail.
    """
    r = _or_empty_str(reason)
    d = _or_empty_str(detail)
    _FOREACH_ERR = frozenset(
        {
            "foreach_assign_keys",
            "foreach_exception",
            "foreach_invalid",
            "foreach_unknown_kind",
        }
    )
    if any((d in _FOREACH_ERR, d == 'foreach_assign_keys', d == 'foreach_exception')):
        if d in ("foreach_assign_keys", "foreach_exception"):
            return "не выполнено: ошибка foreach"
        return "не выполнено: невалидный запрос"
    if r in ("invalid", "invalid_hitl"):
        return "не выполнено: невалидный запрос"
    if r == "has_refutation":
        return "не выполнено: есть опровержение"
    if r == "critic_rejected":
        return "не выполнено: отказ критика"
    if r in ("quota", "reserve", "expansion_limit"):
        return "не выполнено: лимит расширений"
    return "не выполнено: лимит расширений"

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
    r = _or_empty_str(reason)
    if r == TaskReason.IDLE_TIMEOUT.value:
        return IDLE_TIMEOUT_RU
    if r == TaskReason.SANDBOX_TIMEOUT.value:
        return TIMEOUT_BUDGET_RU
    return None

def contributing_reasons_ordered(
    events: list[UnstampedEvent] | list[StoredEvent],
) -> list[str]:
    """TaskReason + ExpansionRefused / foreach / quorum / timeout lines (by seq)."""
    seen: list[str]
    seen = []
    for ev in sorted(events, key=lambda e: _or_zero_int(e.seq)):
        if ev.type == "TaskDone":
            reason = optional_item(ev.payload, "reason")
            if reason and reason not in seen:
                try:
                    TaskReason(reason)
                except ValueError as e:
                    raise JournalFormatError(
                        f"TaskDone.reason not a TaskReason: {reason!r}"
                    ) from e
                seen.append(str(reason))
                line = sandbox_timeout_report_line(
                    str(reason),
                    _or_empty_str(optional_item(ev.payload, 'detail')),
                )
                if line and line not in seen:
                    seen.append(line)
            # PR12: per-task budget line (study-level stays on BudgetExceeded).
            if reason == TaskReason.BUDGET.value:
                from coscientist.core.plan.shares import TASK_BUDGET_EXHAUSTED_LINE

                if TASK_BUDGET_EXHAUSTED_LINE not in seen:
                    seen.append(TASK_BUDGET_EXHAUSTED_LINE)
            line = optional_item(_TASKDONE_DETAIL_LINES, _or_empty_str(optional_item(ev.payload, 'detail')))
            if line and line not in seen:
                seen.append(line)
            if optional_item(ev.payload, "budget_overrun"):
                from coscientist.core.plan.shares import TASK_BUDGET_OVERRUN_LINE

                if TASK_BUDGET_OVERRUN_LINE not in seen:
                    seen.append(TASK_BUDGET_OVERRUN_LINE)
        elif ev.type == "BudgetExceeded":
            from coscientist.core.plan.shares import BUDGET_EXHAUSTED_LINE

            if BUDGET_EXHAUSTED_LINE not in seen:
                seen.append(BUDGET_EXHAUSTED_LINE)
        elif ev.type == "BudgetSharesRefused":
            from coscientist.core.plan.shares import BUDGET_SHARES_REFUSED_LINE

            if BUDGET_SHARES_REFUSED_LINE not in seen:
                seen.append(BUDGET_SHARES_REFUSED_LINE)
        elif ev.type == "ResultRecorded" and optional_item(ev.payload, "budget_overrun"):
            from coscientist.core.plan.shares import TASK_BUDGET_OVERRUN_LINE

            if TASK_BUDGET_OVERRUN_LINE not in seen:
                seen.append(TASK_BUDGET_OVERRUN_LINE)
        elif ev.type == "ExpansionRefused":
            custom = optional_item(_or_empty_dict(ev.payload), "report_line")
            if custom:
                line = str(custom)
            else:
                line = expansion_not_done_line(
                    _or_empty_str(optional_item(ev.payload, 'reason')),
                    _or_empty_str(optional_item(ev.payload, 'detail')),
                )
            if line not in seen:
                seen.append(line)
        elif ev.type == "PlanExpanded" and optional_item(ev.payload, "foreach"):
            _append_foreach_report_lines(seen, ev.payload)
        elif ev.type == "LoopCapped":
            _append_loop_capped_report_line(seen, ev.payload)
        elif ev.type == "LoopRefused":
            _append_loop_refused_report_line(seen, ev.payload)
        elif ev.type == "LoopCompleted":
            _append_loop_completed_report_line(seen, ev.payload)
        elif ev.type == "SideEffectDenied":
            _tmp = _or_empty_str(optional_item(ev.payload, "tool"))
            if _tmp:
                tool = _tmp
            else:
                tool = _UNKNOWN_TOOL
            reason = _or_empty_str(optional_item(ev.payload, 'reason'))
            if reason == "taint_deny":
                line = f"отклонено: tainted контекст ({tool})"
            else:
                line = f"отклонено: побочный эффект ({tool})"
            if line not in seen:
                seen.append(line)
        elif ev.type == "FetchIntegrityRaised":
            if "internal_error" not in seen:
                seen.append("internal_error")
        elif ev.type == "QuestionDisposeRejected":
            custom = optional_item(_or_empty_dict(ev.payload), "report_line")
            if custom:
                line = str(custom)
            else:
                from coscientist.core.claims.dispose import dispose_reject_report_line

                line = dispose_reject_report_line(_or_empty_str(optional_item(ev.payload, 'detail')))
            if line and line not in seen:
                seen.append(line)
        elif ev.type == "EffectIntended":
            # Track for unknown (Intended without Done) pass below.
            pass
    # PR18: EffectIntended without EffectDone → status unknown in the report.
    intended_keys: set[str] = set()
    done_keys: set[str] = set()
    intended_meta: dict[str, dict[str, Any]]
    intended_meta = {}
    for ev in sorted(events, key=lambda e: _or_zero_int(e.seq)):
        if ev.type == "EffectIntended":
            _tmp = optional_item(ev.payload, "key")
            if _tmp is not None:
                k = str(_tmp)
            else:
                k = f"{ev.entity_key}"
            intended_keys.add(k)
            intended_meta[k] = dict(ev.payload)
        elif ev.type == "EffectDone":
            _k = optional_item(ev.payload, "key")
            if _k is not None:
                done_keys.add(str(_k))
            else:
                done_keys.add(f"{ev.entity_key}")
    for k in sorted(intended_keys - done_keys):
        meta = optional_item(intended_meta, k)
        if meta is None:
            meta = {}
        _tmp = _or_empty_str(optional_item(meta, "tool"))
        if _tmp:
            tool = _tmp
        else:
            tool = _UNKNOWN_TOOL
        line = f"эффект неизвестен: {tool}"
        if line not in seen:
            seen.append(line)
    return seen

def _require_survivors(p: dict[str, Any], *, what: str) -> int:
    try:
        raw = p["survivors"]
    except KeyError as e:
        raise JournalFormatError(f"{what}: missing 'survivors'") from e
    return require_exact_int(raw, what=f"{what}.survivors")


def _append_loop_capped_report_line(seen: list[str], p: dict[str, Any]) -> None:
    """DESIGN_M1 §4: iteration_cap vs refuse/error (never disguise bugs as cap)."""
    from coscientist.core.plan.loop import (
        LOOP_CAPPED_REASONS,
        LOOP_REASON_ERROR,
        LOOP_REASON_REFUSED,
        _require_loop_iter,
        _require_loop_reason,
    )

    i = _require_loop_iter(p, what="LoopCapped")
    reason = _require_loop_reason(
        p, key="reason", allowed=LOOP_CAPPED_REASONS, what="LoopCapped"
    )
    if "detail" in p:
        detail = _or_empty_str(p["detail"])
    else:
        detail = ""
    # Classify only by validated journal reason — never by detail text.
    if reason in (LOOP_REASON_ERROR, LOOP_REASON_REFUSED):
        if detail:
            _msg = detail
        else:
            _msg = reason
        line = f"цикл остановлен ошибкой после {i} итераций: {_msg}"
        if line not in seen:
            seen.append(line)
        if reason not in seen:
            seen.append(reason)
        return
    m = _require_survivors(p, what="LoopCapped")
    line = f"цикл остановлен лимитом после {i} итераций, осталось {m}"
    if line not in seen:
        seen.append(line)
    if "iteration_cap" not in seen:
        seen.append("iteration_cap")

def _append_loop_refused_report_line(seen: list[str], p: dict[str, Any]) -> None:
    """LoopRefused: honest error/refuse line (not iteration_cap)."""
    from coscientist.core.plan.loop import (
        LOOP_REFUSED_REASONS,
        _require_loop_iter,
        _require_loop_reason,
    )

    i = _require_loop_iter(p, what="LoopRefused")
    reason = _require_loop_reason(
        p, key="reason", allowed=LOOP_REFUSED_REASONS, what="LoopRefused"
    )
    if "detail" in p:
        detail = _or_empty_str(p["detail"])
    else:
        detail = ""
    if detail:
        _msg = detail
    else:
        _msg = reason
    line = f"цикл остановлен ошибкой после {i} итераций: {_msg}"
    if line not in seen:
        seen.append(line)
    if reason not in seen:
        seen.append(reason)

def _append_loop_completed_report_line(seen: list[str], p: dict[str, Any]) -> None:
    """Until-met completion line (journaled LoopCompleted)."""
    from coscientist.core.plan.loop import _require_loop_iter

    i = _require_loop_iter(p, what="LoopCompleted")
    m = _require_survivors(p, what="LoopCompleted")
    line = f"цикл завершён после {i} итераций, осталось {m}"
    if line not in seen:
        seen.append(line)

def _append_foreach_report_lines(seen: list[str], p: dict[str, Any]) -> None:
    items = _or_empty_list(optional_item(p, "foreach_items"))
    added = _or_empty_list(optional_item(p, "added_tasks"))
    dropped = [_or_empty_str(x) for x in _or_empty_list(optional_item(p, "foreach_dropped"))]
    total = optional_item(p, "foreach_total")
    if total is None:
        total = len(items) + len(dropped)
    refute = "не выполнено: проверка опровержения не выполнена"
    if not items and not added:
        if "не выполнено: foreach пуст" not in seen:
            seen.append("не выполнено: foreach пуст")
        if optional_item(p, "foreach_refute") and refute not in seen:
            seen.append(refute)
        return
    line = f"foreach: развёрнуто {len(added)}"
    if line not in seen:
        seen.append(line)
    if any((optional_item(p, 'foreach_capped'), dropped)):
        cap = f"ограничение foreach: взято {len(added)} из {total}"
        if dropped:
            cap += f", отброшены {', '.join(dropped)}"
        if cap not in seen:
            seen.append(cap)
    if optional_item(p, "foreach_refute"):
        for did in dropped:
            r = f"{refute} ({did})"
            if r not in seen:
                seen.append(r)
    for n in _or_empty_list(optional_item(p, "foreach_rank_notes")):
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
    if trigger not in _TRIGGER_TERMINAL:
        raise ValueError(f"unknown finalize trigger: {trigger!r}")
    term = _TRIGGER_TERMINAL[trigger]
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

    Conflict / StudyClosedError on the StudyFinalized write is success only when
    that event is already in the store (commit succeeded, fold/local project
    failed, or a peer finalized first). Otherwise the error is re-raised —
    never mark finalized without the event.
    """
    if any((runtime.study is None, is_finalized(runtime))):
        return
    sid = runtime.study.study_id
    refusal = _refusal_payload(runtime, trigger)
    try:
        payload = await _build_rich_payload(runtime, sid=sid, trigger=trigger)
    except LeaseLost:
        raise
    except Exception as e:  # noqa: BLE001  # audit-ok[broad_except]: finalize.py L531 broad_except structural path not inventing domain value
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
    except (Conflict, StudyClosedError) as err:
        await _sync_finalized_from_store(runtime, sid=sid, err=err)
    except Exception as e:  # noqa: BLE001  # audit-ok[broad_except]: finalize.py L545 broad_except structural path not inventing domain value
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
        except (Conflict, StudyClosedError) as err:
            await _sync_finalized_from_store(runtime, sid=sid, err=err)
    runtime._finalized = True  # noqa: SLF001

async def _sync_finalized_from_store(
    runtime: StudyRuntime, *, sid: str, err: BaseException
) -> None:
    """On Conflict/StudyClosedError: succeed only if StudyFinalized idem exists."""
    expected = make_idem("StudyFinalized", sid)
    for ev in await runtime.store.read_all():
        if ev.idem_key == expected:
            if not is_finalized(runtime):
                runtime._project_local(ev)  # noqa: SLF001
            return
    raise err

async def _append_finalized(runtime: StudyRuntime, *, sid: str, payload: dict[str, Any]) -> None:
    

    now = runtime.clock.now()
    await runtime._append(  # noqa: SLF001
        [
            UnstampedEvent(
                study_id=sid,
                type="StudyFinalized",
                entity_key=sid,
                idem_key=make_idem("StudyFinalized", sid),
                payload=payload,
                ts=now,
                schema_version=STUDY_FINALIZED_SCHEMA_VERSION,
            )
        ]
    )

def truncate_contributing_reasons(reasons: list[str] | None, *, limit: int) -> list[str]:
    """Truncate contributing_reasons while keeping timeout RU lines.

    ``TIMEOUT_BUDGET_RU`` / ``IDLE_TIMEOUT_RU`` are non-truncatable: they are
    always retained (deduped, original relative order). Other reasons fill the
    remaining slots in first-seen order. If only protected lines remain and
    somehow exceed ``limit``, collapse duplicates into a single counter line.
    """
    src = [_or_empty_str(r) for r in _or_empty_list(reasons)]
    if len(src) <= limit:
        return src
    pinned: list[str]
    pinned = []
    pinned_seen: set[str] = set()
    others: list[str]
    others = []
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
    raw_qdisp = optional_item(out, "question_dispositions")
    qdisp_src = _or_empty_dict(raw_qdisp)
    qdisp = {}
    for qid, d in qdisp_src.items():
        if isinstance(d, dict):
            row = dict(d)
            form = _or_empty_str(optional_item(row, "formulation"))
            row["formulation"] = form[:80]
        else:
            row = {}
            row["formulation"] = ""
        qdisp[qid] = row
    out["question_dispositions"] = qdisp
    out["contributing_reasons"] = truncate_contributing_reasons(
        _or_empty_list(optional_item(out, "contributing_reasons")),
        limit=CONTRIB_REASONS_LIMIT,
    )
    if len(canonical_json(out)) <= MAX_FINALIZED_PAYLOAD_BYTES:
        return out
    raise RuntimeError("finalized_payload_too_large")

def _artifact_lookup(runtime: StudyRuntime) -> Any | None:
    """``get_meta`` when an artifact store is attached (live replay); else None."""
    arts = runtime.artifacts
    if arts is None:
        ex = runtime.executor
        if ex is None:
            arts = None
        else:
            arts = ex.artifacts
    if arts is None:
        return None
    try:
        lookup = arts.get_meta
    except AttributeError:
        return None  # audit-ok[except_silent_return]: finalize.py L663 except_silent_return structural path not inventing domain value
    if callable(lookup):
        return lookup
    else:
        return None

async def _build_rich_payload(
    runtime: StudyRuntime, *, sid: str, trigger: FinalizeTrigger
) -> dict[str, Any]:
    # Reclassified fetch tools: journal diagnostic so contrib includes internal_error;
    # paired SourceFetched stays uncounted (fold catches FetchToolReclassified).
    from coscientist.core.events.projections import fold_events
    from coscientist.core.execution.source_fetch import reconcile_fetch_integrity

    try:
        await reconcile_fetch_integrity(runtime.writer)
    except LeaseLost:
        raise
    except Exception:  # noqa: BLE001  # audit-ok[broad_except]: finalize.py L681 broad_except structural path not inventing domain value
        log.warning("reconcile_fetch_integrity failed", exc_info=True)

    plans = await _load_plans(runtime)
    lookup = _artifact_lookup(runtime)
    from coscientist.core.plan.loop import capped_scope_questions

    qdisp = dispositions(
        tasks=runtime.tasks,
        plans=plans,
        graph=runtime.graph,
        study=runtime.study,
        results=runtime.results,
        artifact_lookup=lookup,
        loop_capped_questions=capped_scope_questions(runtime),
        question_disposals=runtime.question_disposals,
        journal_kinds=runtime.artifact_kinds,
    )
    hyp_verdicts, hyp_unavail = report_hypothesis_verdicts(
        graph=runtime.graph,
        study=runtime.study,
        results=runtime.results,
        tasks=runtime.tasks,
        artifact_lookup=lookup,
    )
    term = terminal_reason_for(trigger, tasks=runtime.tasks, question_dispositions=qdisp)
    events = await runtime.store.read_all()
    contrib = contributing_reasons_ordered(events)
    # If journaling failed/skipped, fold still surfaces fetch_integrity → reason.
    if "internal_error" not in contrib:
        folded = fold_events(events)
        if optional_item(folded, "fetch_integrity"):
            contrib.append("internal_error")
    # PR15: stamped ArtifactRegistered with missing meta on replay → tainted mark.
    if "internal_error" not in contrib and runtime._stamp_meta_missing:
        contrib.append("internal_error")
    # PR13: disposition report_line (e.g. «не выполнено: рубрика не принята»).
    # Note: legacy dual StudyFinalized is *not* folded into contrib here — finalize()
    # returns early when already finalized, so that taint lives in meta / local fold only.
    for d in qdisp.values():
        if isinstance(d, dict):
            line = optional_item(d, "report_line")
        else:
            line = None
        if line and line not in contrib:
            contrib.append(str(line))
    # PlanRefused / invalid final_type lines folded from events.
    for ev in events:
        if ev.type == "PlanRefused":
            line = optional_item(_or_empty_dict(ev.payload), "report_line")
            if line is None:
                line = "не выполнено: план отклонён"
            if line not in contrib:
                contrib.append(_or_empty_str(line))
        if ev.type == "JudgeCasCorrupt":
            line = optional_item(_or_empty_dict(ev.payload), "report_line")
            if line is None:
                line = "не выполнено: внутренняя ошибка (judge CAS)"
            if line not in contrib:
                contrib.append(_or_empty_str(line))
    # Upcaster-coerced unknown final_type → report line on plan questions.
    if isinstance(plans, dict):
        plan_rows = list(plans.values())
    else:
        plan_rows = _or_empty_list(plans)
    for plan in plan_rows:
        if isinstance(plan, dict):
            qs = optional_item(plan, "questions")
        else:
            qs = plan.questions
        for q in _or_empty_list(qs):
            if isinstance(q, dict):
                line = optional_item(q, "final_type_report_line")
                if line and line not in contrib:
                    contrib.append(_or_empty_str(line))
    study = runtime.study
    if not (study is not None):
        raise RuntimeError('assertion failed: study is not None')
    # PR13 §6.1: final_types into study_view; view uses study_fulfilled (single source).
    from coscientist.core.claims.rubric import question_final_types

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
    verdict = view.verdict
    if verdict is None:
        verdict = 'partial'
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
    local = runtime.plans
    if isinstance(local, dict) and local:
        return local
    rows = await runtime.store.get_projection("plans")
    out: dict[str, dict[str, Any]]
    out = {}
    for i, r in enumerate(rows):
        pid = _or_empty_str(optional_item(r, "plan_id"))
        if not pid:
            pid = _or_empty_str(i)
        out[pid] = r
    return out

def _refusal_payload(runtime: StudyRuntime, trigger: FinalizeTrigger) -> str | None:
    if trigger != "intake":
        return None
    ref = runtime._intake_refusal
    if ref is None:
        return None
    from enum import Enum

    kind = ref.kind
    reason = ref.reason
    if isinstance(kind, Enum):
        kv = kind.value
    else:
        kv = kind
    if kv:
        return f"{kv}:{reason}"
    return _or_empty_str(reason)

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
    except LeaseLost:
        raise
    except Exception as e:  # noqa: BLE001  # audit-ok[broad_except]: finalize.py L852 broad_except structural path not inventing domain value
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
                hypotheses=_or_empty_dict(hypothesis_verdicts),
                hypotheses_unavailable=hypotheses_unavailable,
                custom_checks=_safe_custom_checks(runtime),
                observations=_or_empty_list(runtime._observations),
            )
        except LeaseLost:
            raise
        except Exception as e2:  # noqa: BLE001  # audit-ok[broad_except]: finalize.py L874 broad_except structural path not inventing domain value
            raise RuntimeError(f"minimal_report:{type(e2).__name__}") from e2
    custom_checks = _safe_custom_checks(runtime)
    # Findings outside the plan stay unverified observations (DESIGN_M1 §16).
    observations = _or_empty_list(runtime._observations)
    if minimal is not None and isinstance(minimal, dict):
        minimal = dict(minimal)
        # Prefer already-truncated lists from build_minimal_report when present.
        if not optional_item(minimal, "custom_checks"):
            minimal["custom_checks"] = custom_checks
        if not optional_item(minimal, "observations"):
            minimal["observations"] = observations
    return {
        "trigger": trigger,
        "terminal_reason": term.value,
        "contributing_reasons": contrib,
        "verdict": verdict,
        "question_dispositions": qdisp,
        "hypothesis_verdicts": _or_empty_dict(hypothesis_verdicts),
        "hypotheses_unavailable": hypotheses_unavailable,
        "report_artifact_id": report_artifact_id,
        "conclusion_ids": conclusion_ids,
        "minimal_report": minimal,
        "fallback_reason": fallback,
        "refusal": refusal,
        "custom_checks": custom_checks,
        "observations": observations,
    }

def _safe_custom_checks(runtime: Any) -> list[dict[str, Any]]:
    """Isolate custom_checks_report — never block StudyFinalized on report bugs."""
    try:
        from coscientist.core.plan.proposed_check import custom_checks_report

        return list(custom_checks_report(runtime))
    except LeaseLost:
        raise
    except Exception as e:  # noqa: BLE001  # audit-ok[broad_except]: finalize.py L911 broad_except structural path not inventing domain value
        log.warning("custom_checks_report failed: %s", e, exc_info=True)
        return []  # audit-ok[except_silent_return]: finalize.py L913 except_silent_return structural path not inventing domain value

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
    study = runtime.study
    if study is not None:
        as_of_raw = study.as_of
    else:
        as_of_raw = None
    as_of_s = None
    if as_of_raw is not None:
        try:
            as_of_s = as_of_raw.isoformat()
        except AttributeError:
            as_of_s = _or_empty_str(as_of_raw)
    plans_local = runtime.plans
    md = render_markdown_report(
        study_id=sid,
        terminal_reason=term.value,
        trigger=trigger,
        verdict=verdict,
        contributing_reasons=contrib,
        question_dispositions=qdisp,
        tasks=runtime.tasks,
        refusal=refusal,
        as_of=as_of_s,
        plans=_dict_or_none(plans_local),
        custom_checks=_safe_custom_checks(runtime),
        observations=_or_empty_list(runtime._observations),
    )
    # Surface infra details (sandbox reason + soft-isolation note) in report text.
    # Escape agent/host-provided strings so markdown/HTML injection cannot reshape
    # the report structure.
    results = runtime.results
    if results is None:
        results = {}
    detail_lines: list[str]
    detail_lines = []
    for rid in sorted(results):
        res = results[rid]
        if not isinstance(res, dict):
            continue
        detail = optional_item(res, "detail")
        if detail:
            detail_lines.append(f"- result `{rid}` detail: {_escape_report_text(detail)}")
        reason = optional_item(res, "reason")
        if reason:
            detail_lines.append(f"- result `{rid}` reason: {_escape_report_text(reason)}")
    if detail_lines:
        md = md + "\n## Infra / result details\n" + "\n".join(detail_lines) + "\n"
    arts = _artifact_store(runtime)
    if arts is None:
        raise CASUnavailable("no artifact store on runtime")
    from coscientist.core.execution.artifacts import make_tool_ref

    art = arts.put(
        md.encode("utf-8"),
        make_tool_ref(f"finalize:{sid}", attempt_id=f"finalize:{sid}"),
        name="report.md",
        media_type="text/markdown",
    )
    return art.artifact_id

def _artifact_store(runtime: StudyRuntime) -> Any:
    ex = runtime.executor
    if ex is not None and ex.artifacts is not None:
        return ex.artifacts
    return runtime.artifacts
