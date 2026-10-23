"""Study finalizer: StudyFinalized with report or minimal_report (total)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from coscientist.core.dispositions import dispositions
from coscientist.core.domain import Task, TaskOutcome, TaskState
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.graph import GraphEdge, GraphNode
from coscientist.core.ids import conclusion_id, report_node_id
from coscientist.core.minimal_report import build_minimal_report, render_markdown_report
from coscientist.core.reasons import TaskReason, TerminalReason
from coscientist.core.study_view import study_view

if TYPE_CHECKING:
    from coscientist.core.machine import StudyRuntime

log = logging.getLogger(__name__)

FinalizeTrigger = Literal["cancel", "budget", "deadline", "all_done", "intake"]


def is_finalized(runtime: StudyRuntime) -> bool:
    return bool(getattr(runtime, "_finalized", False))


def pick_finalize_trigger(
    *,
    cancelled: bool = False,
    budget: bool = False,
    deadline: bool = False,
    all_done: bool = False,
) -> FinalizeTrigger | None:
    """Same-tick tie-break: cancelled > budget > deadline > all_done (§0.2)."""
    if cancelled:
        return "cancel"
    if budget:
        return "budget"
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
    # all_done
    req = [t for t in tasks.values() if t.spec.required and t.state == TaskState.DONE]
    if any(
        t.outcome == TaskOutcome.FAILED and t.reason == TaskReason.HUMAN_TIMEOUT
        for t in req
    ):
        return TerminalReason.HUMAN_TIMEOUT
    # infeasible requires disposition ∈ {infeasible, ill_posed} — not reachable in PR 2
    _ = question_dispositions
    return TerminalReason.COMPLETED


def contributing_reasons_ordered(
    events: list[EventEnvelope],
) -> list[str]:
    """TaskReason values from TaskDone payloads, first-seen order by seq."""
    seen: list[str] = []
    for ev in sorted(events, key=lambda e: e.seq or 0):
        if ev.type != "TaskDone":
            continue
        reason = ev.payload.get("reason")
        if reason and reason not in seen:
            try:
                TaskReason(reason)
            except ValueError:
                continue
            seen.append(str(reason))
    return seen


async def finalize(runtime: StudyRuntime, *, trigger: FinalizeTrigger) -> None:
    """Write StudyFinalized (idempotent). Never raises past minimal_report fallback."""
    if runtime.study is None or is_finalized(runtime):
        return
    sid = runtime.study.study_id
    plans = await _load_plans(runtime)
    qdisp = dispositions(tasks=runtime.tasks, plans=plans)
    term = terminal_reason_for(
        trigger, tasks=runtime.tasks, question_dispositions=qdisp
    )
    events = await runtime.store.read_all()
    contrib = contributing_reasons_ordered(events)
    view = study_view(
        runtime.tasks.values(),
        open_hitl=0,
        attempts=runtime.attempts,
        finalized=False,
        now=runtime.clock.now(),
        effective_deadline=runtime.study.effective_deadline(),
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
    )
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
                schema_version=1,
            )
        ]
    )
    runtime._finalized = True  # noqa: SLF001


async def _load_plans(runtime: StudyRuntime) -> dict[str, dict[str, Any]]:
    local = getattr(runtime, "plans", None)
    if isinstance(local, dict) and local:
        return local
    rows = await runtime.store.get_projection("plans")
    return {str(r.get("plan_id") or i): r for i, r in enumerate(rows)}


async def _build_finalized_payload(
    runtime: StudyRuntime,
    *,
    sid: str,
    trigger: FinalizeTrigger,
    term: TerminalReason,
    contrib: list[str],
    verdict: str,
    qdisp: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    fallback: str | None = None
    report_artifact_id: str | None = None
    conclusion_ids: list[str] = []
    minimal: dict[str, Any] | None = None
    try:
        report_artifact_id, conclusion_ids = await _primary_report_path(
            runtime, sid=sid, trigger=trigger, term=term, verdict=verdict, qdisp=qdisp
        )
    except Exception as e:  # noqa: BLE001 — totality: any failure → minimal_report
        fallback = type(e).__name__
        log.warning("finalize primary path failed: %s", e, exc_info=True)
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
        )
    return {
        "trigger": trigger,
        "terminal_reason": term.value,
        "contributing_reasons": contrib,
        "verdict": verdict,
        "question_dispositions": qdisp,
        "report_artifact_id": report_artifact_id,
        "conclusion_ids": conclusion_ids,
        "minimal_report": minimal,
        "fallback_reason": fallback,
    }


async def _primary_report_path(
    runtime: StudyRuntime,
    *,
    sid: str,
    trigger: str,
    term: TerminalReason,
    verdict: str,
    qdisp: dict[str, dict[str, Any]],
) -> tuple[str, list[str]]:
    md = render_markdown_report(
        study_id=sid,
        terminal_reason=term.value,
        trigger=trigger,
        verdict=verdict,
        contributing_reasons=[],
        question_dispositions=qdisp,
        tasks=runtime.tasks,
    )
    arts = _artifact_store(runtime)
    if arts is None:
        raise RuntimeError("CASUnavailable")
    from coscientist.core.artifacts import make_tool_ref

    art = arts.put(
        md.encode("utf-8"),
        make_tool_ref(f"finalize:{sid}"),
        name="report.md",
        media_type="text/markdown",
    )
    conclusion_ids = _emit_conclusion_nodes(runtime, sid=sid, qdisp=qdisp, term=term)
    _emit_report_node(runtime, sid=sid, artifact_id=art.artifact_id)
    return art.artifact_id, conclusion_ids


def _artifact_store(runtime: StudyRuntime) -> Any:
    ex = getattr(runtime, "executor", None)
    if ex is not None and getattr(ex, "artifacts", None) is not None:
        return ex.artifacts
    return getattr(runtime, "artifacts", None)


def _emit_conclusion_nodes(
    runtime: StudyRuntime,
    *,
    sid: str,
    qdisp: dict[str, dict[str, Any]],
    term: TerminalReason,
) -> list[str]:
    """Upsert conclusion nodes + answers edges into the in-memory graph."""
    now = runtime.clock.now()
    ids: list[str] = []
    for qid, d in sorted(qdisp.items()):
        cid = conclusion_id()
        ids.append(cid)
        runtime.graph.upsert_node(
            GraphNode(
                node_id=cid,
                kind="conclusion",
                study_id=sid,
                ref_id=cid,
                attrs={
                    "synthesis": f"not_attempted:{d.get('reason')}",
                    "validity_bounds": "core PR2: no counting claims",
                    "question_id": qid,
                },
                created_at=now,
            )
        )
        q_node = f"{sid}:question:{qid}"
        if runtime.graph.get_node(q_node) is None:
            runtime.graph.upsert_node(
                GraphNode(
                    node_id=q_node,
                    kind="question",
                    study_id=sid,
                    ref_id=qid,
                    attrs={"formulation": d.get("formulation", "")},
                    created_at=now,
                )
            )
        runtime.graph.add_edge(
            GraphEdge(
                edge_id=f"answers:{cid}:{qid}",
                kind="answers",
                study_id=sid,
                src_id=cid,
                dst_id=q_node,
                created_at=now,
            )
        )
    # study-level conclusion
    scid = conclusion_id()
    ids.append(scid)
    runtime.graph.upsert_node(
        GraphNode(
            node_id=scid,
            kind="conclusion",
            study_id=sid,
            ref_id=scid,
            attrs={"synthesis": f"terminal:{term.value}", "validity_bounds": "study"},
            created_at=now,
        )
    )
    study_node = f"{sid}:study"
    if runtime.graph.get_node(study_node) is not None:
        runtime.graph.add_edge(
            GraphEdge(
                edge_id=f"answers:{scid}:study",
                kind="answers",
                study_id=sid,
                src_id=scid,
                dst_id=study_node,
                created_at=now,
            )
        )
    return ids


def _emit_report_node(runtime: StudyRuntime, *, sid: str, artifact_id: str) -> None:
    now = runtime.clock.now()
    rid = report_node_id()
    runtime.graph.upsert_node(
        GraphNode(
            node_id=rid,
            kind="report",
            study_id=sid,
            ref_id=rid,
            attrs={"artifact_id": artifact_id, "format": "text/markdown"},
            created_at=now,
        )
    )
