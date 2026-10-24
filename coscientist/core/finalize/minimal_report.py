"""Deterministic minimal_report builder (≤ 10 KiB after JCS)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from coscientist.core.claims.reasons import TaskReason
from coscientist.core.events.digest import canonical_json
from coscientist.core.events.errors import JournalFormatError, require_journal_field
from coscientist.core.execution.domain import Task, TaskOutcome, TaskState

MAX_BYTES = 10_240


def _task_provenance(task: Task, attempts: Mapping[str, Mapping[str, Any]]) -> str:
    """Provenance of the latest closed attempt.

    Zero-attempt tasks (cancelled/skipped before start) report ``stub`` because
    no attempt row exists — not a missing-field default on a journaled attempt.
    Closed attempts must stamp ``provenance`` (JournalFormatError if absent).
    """
    if not task.attempts:
        return "stub"
    for aid in reversed(task.attempts):
        if aid not in attempts:
            raise JournalFormatError(
                f"missing attempt row {aid!r} for task {task.spec.task_key!r}",
                field="attempts",
            )
        att = attempts[aid]
        if not require_journal_field(att, "closed", ctx=aid):
            continue
        return str(require_journal_field(att, "provenance", ctx=aid))
    raise JournalFormatError(
        f"no closed attempt provenance for task {task.spec.task_key!r}",
        field="provenance",
    )


def _summary(
    terminal_reason: str,
    tasks: list[Task],
    questions: list[dict[str, Any]],
) -> str:
    n = len(tasks)
    counts = {o.value: 0 for o in TaskOutcome}
    for t in tasks:
        if t.outcome is not None:
            counts[t.outcome.value] += 1
    m = len(questions)
    k = sum(1 for q in questions if q.get("disposition") == "not_attempted")
    return (
        f"Study закрыто: {terminal_reason}; задач {n} "
        f"(SUCCESS {counts['SUCCESS']}, PARTIAL {counts['PARTIAL']}, "
        f"FAILED {counts['FAILED']}, SKIPPED {counts['SKIPPED']}); "
        f"вопросов {m}, not_attempted {k}"
    )


def _sections(
    tasks: list[Task], attempts: Mapping[str, Mapping[str, Any]]
) -> dict[str, list[str]]:
    real: list[str] = []
    stub: list[str] = []
    human: list[str] = []
    for t in tasks:
        key = t.spec.task_key
        prov = _task_provenance(t, attempts)
        if t.outcome in {TaskOutcome.SUCCESS, TaskOutcome.PARTIAL} and prov == "real":
            real.append(key)
        if prov in {"partial", "stub"}:
            stub.append(key)
        if t.outcome in {TaskOutcome.FAILED, TaskOutcome.SKIPPED} and t.reason:
            human.append(key)
    return {"real": real, "stub": stub, "human": human}


def build_minimal_report(
    *,
    study_id: str,
    terminal_reason: str,
    trigger: str,
    contributing_reasons: list[str],
    verdict: str,
    tasks: Mapping[str, Task],
    attempts: Mapping[str, Mapping[str, Any]],
    question_dispositions: Mapping[str, Mapping[str, Any]],
    fallback_reason: str | None,
    refusal: str | None = None,
    hypotheses: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]] | None = None,
    hypotheses_unavailable: str | None = None,
    custom_checks: Sequence[Mapping[str, Any]] | None = None,
    observations: Sequence[Mapping[str, Any] | str] | None = None,
) -> dict[str, Any]:
    task_list = sorted(tasks.values(), key=lambda t: t.spec.task_key)
    q_rows = [
        {
            "question_id": qid,
            "formulation": str(d.get("formulation", ""))[:500],
            "disposition": str(require_journal_field(d, "disposition", ctx=qid)),
            "reason": str(d.get("reason", ""))[:200],
        }
        for qid, d in sorted(question_dispositions.items())
    ]
    if isinstance(hypotheses, Mapping):
        h_rows: list[dict[str, Any]] = [
            dict(v) if isinstance(v, Mapping) else {"hypothesis_id": hid, "verdict": v}
            for hid, v in sorted(hypotheses.items())
        ]
    else:
        h_rows = [dict(h) for h in (hypotheses or [])]
    t_rows = [
        {
            "task_key": t.spec.task_key,
            "kind": t.kind.value,
            "outcome": t.outcome.value if t.outcome else TaskOutcome.SKIPPED.value,
            "reason": (
                t.reason.value
                if isinstance(t.reason, TaskReason)
                else (str(t.reason) if t.reason else None)
            ),
            "provenance": _task_provenance(t, attempts),
        }
        for t in task_list
        if t.state == TaskState.DONE or t.outcome is not None
    ]
    q_n = len(q_rows)
    na_n = sum(1 for q in q_rows if q.get("disposition") == "not_attempted")
    report: dict[str, Any] = {
        "study_id": study_id,
        "terminal_reason": terminal_reason,
        "trigger": trigger,
        "contributing_reasons": contributing_reasons[:20],
        "summary": _summary(terminal_reason, task_list, q_rows),
        "question_count": q_n,
        "not_attempted_count": na_n,
        "fallback_reason": fallback_reason,
        "refusal": refusal,
        "verdict": verdict,
        "tasks": t_rows,
        "questions": q_rows,
        "hypotheses": h_rows,
        "hypotheses_unavailable": hypotheses_unavailable,
        "sections": _sections(task_list, attempts),
        # Apply custom_checks/observations BEFORE truncate so they respect 10KB.
        "custom_checks": [dict(c) for c in (custom_checks or [])],
        "observations": list(observations or []),
        "truncated": False,
    }
    return truncate_minimal_report(report)


def truncate_minimal_report(report: dict[str, Any]) -> dict[str, Any]:
    """Shrink lists until JCS size ≤ MAX_BYTES; summary counters stay intact."""
    out = dict(report)
    out["truncated"] = False
    if len(canonical_json(out)) <= MAX_BYTES:
        return out
    out["truncated"] = True
    qs = [dict(q) for q in out.get("questions", [])]
    for q in qs:
        q["formulation"] = str(q.get("formulation", ""))[:80]
    out["questions"] = qs
    if len(canonical_json(out)) <= MAX_BYTES:
        return out
    while len(canonical_json(out)) > MAX_BYTES and out.get("tasks"):
        out["tasks"] = out["tasks"][:-1]
    while len(canonical_json(out)) > MAX_BYTES and out.get("questions"):
        out["questions"] = out["questions"][:-1]
    # SPEC: truncate hypotheses after questions.
    while len(canonical_json(out)) > MAX_BYTES and out.get("hypotheses"):
        out["hypotheses"] = out["hypotheses"][:-1]
    # PR20: custom_checks / observations participate in the 10KB budget.
    while len(canonical_json(out)) > MAX_BYTES and out.get("observations"):
        out["observations"] = out["observations"][:-1]
    while len(canonical_json(out)) > MAX_BYTES and out.get("custom_checks"):
        out["custom_checks"] = out["custom_checks"][:-1]
    secs = dict(out.get("sections") or {})
    for key in ("human", "stub", "real"):
        while len(canonical_json(out)) > MAX_BYTES and secs.get(key):
            secs[key] = secs[key][:-1]
            out["sections"] = secs
    return out


def render_markdown_report(
    *,
    study_id: str,
    terminal_reason: str,
    trigger: str,
    verdict: str,
    contributing_reasons: list[str],
    question_dispositions: Mapping[str, Mapping[str, Any]],
    tasks: Mapping[str, Task],
    refusal: str | None = None,
    as_of: str | None = None,
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]] | None = None,
    custom_checks: Sequence[Mapping[str, Any]] | None = None,
    observations: Sequence[Mapping[str, Any] | str] | None = None,
) -> str:
    """Deterministic MD report (no LLM)."""
    lines = [
        f"# Study report `{study_id}`",
        "",
        f"- trigger: `{trigger}`",
        f"- terminal_reason: `{terminal_reason}`",
        f"- verdict: `{verdict}`",
        f"- contributing_reasons: {', '.join(contributing_reasons) or '(none)'}",
    ]
    if as_of:
        lines.append(f"- as_of: `{as_of}`")
    if refusal:
        lines.append(f"- refusal: `{refusal}`")
    not_done = [c for c in contributing_reasons if str(c).startswith("не выполнено:")]
    for line in not_done:
        lines.append(f"- {line}")
    lines.extend(
        [
            "",
            "## Questions",
        ]
    )
    if not question_dispositions:
        lines.append("_No questions._")
    for qid, d in sorted(question_dispositions.items()):
        extra = f"; note={d['note']}" if d.get("note") else ""
        lines.append(
            f"- `{qid}`: {d.get('disposition')} ({d.get('reason')}) — "
            f"{d.get('formulation', '')}{extra}"
        )
    lines.extend(["", "## Tasks"])
    for t in sorted(tasks.values(), key=lambda x: x.spec.task_key):
        lines.append(
            f"- `{t.spec.task_key}` [{t.kind.value}] "
            f"{t.state.value}/{t.outcome} reason={t.reason}"
        )
    tree = render_subtask_tree(
        question_dispositions=question_dispositions,
        tasks=tasks,
        plans=plans,
    )
    if tree:
        lines.extend(["", "## Дерево подзадач", *tree])
    # PR20: non-catalog proposed checks + unverified out-of-plan observations.
    lines.extend(["", "## Custom checks"])
    if not custom_checks:
        lines.append("_None._")
    else:
        for c in custom_checks:
            who = c.get("approved_by") or c.get("rejected_by") or "?"
            delg = "delegated" if c.get("delegated") else "human"
            from coscientist.core.finalize.finalize import _escape_report_text

            measures = _escape_report_text(c.get("measures") or "")
            lines.append(
                f"- `{c.get('proposal_id')}` status={c.get('status')} "
                f"by={who}/{delg} — {measures} (не каталожная)"
            )
    lines.extend(["", "## Observations"])
    if not observations:
        lines.append("_None._")
    else:
        for obs in observations:
            if isinstance(obs, Mapping):
                lines.append(f"- {obs.get('text') or obs}")
            else:
                lines.append(f"- {obs}")
    return "\n".join(lines) + "\n"


def render_subtask_tree(
    *,
    question_dispositions: Mapping[str, Mapping[str, Any]],
    tasks: Mapping[str, Task],
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Markdown lines for «Дерево подзадач» (DESIGN_M1 §10 / R29).

    question → subquestion → task_key with status/outcome/reason, origin,
    revision; superseded task keys struck through.
    """
    if not plans:
        return []
    from coscientist.core.plan.decomposes import (
        children_map,
        parent_map_from_plans,
        question_origin_from_plans,
        question_revision_from_plans,
        superseded_task_keys_from_plans,
    )
    from coscientist.core.plan.question_tasks import question_tasks

    parent_of = parent_map_from_plans(plans)
    if not any(parent_of.values()):
        return []
    kids = children_map(parent_of)
    origins = question_origin_from_plans(plans)
    revs = question_revision_from_plans(plans)
    cover = question_tasks(plans)
    superseded = superseded_task_keys_from_plans(plans)
    by_key = {t.spec.task_key: t for t in tasks.values()}
    roots = sorted(qid for qid, p in parent_of.items() if not p)
    lines: list[str] = []

    def _emit_question(qid: str, indent: int) -> None:
        d = question_dispositions.get(qid) or {}
        if qid not in origins:
            raise JournalFormatError(
                f"missing question origin for {qid!r}",
                field="origin",
            )
        if qid not in revs:
            raise JournalFormatError(
                f"missing question revision for {qid!r}",
                field="revision",
            )
        origin = origins[qid]
        rev = revs[qid]
        disp = require_journal_field(d, "disposition", ctx=qid)
        reason = d.get("reason")
        pad = "  " * indent
        lines.append(
            f"{pad}- `{qid}` origin={origin} rev={rev}: {disp}"
            + (f" ({reason})" if reason else "")
        )
        for tkey in sorted(cover.get(qid) or ()):
            t = by_key.get(tkey)
            if t is None and tkey in superseded:
                lines.append(f"{pad}  - ~~`{tkey}`~~ superseded")
                continue
            if t is None:
                lines.append(f"{pad}  - `{tkey}` (missing)")
                continue
            key_fmt = f"~~`{tkey}`~~" if tkey in superseded else f"`{tkey}`"
            lines.append(
                f"{pad}  - {key_fmt}: {t.state.value}/{t.outcome} reason={t.reason}"
            )
        for child in kids.get(qid) or []:
            _emit_question(child, indent + 1)

    for root in roots:
        _emit_question(root, 0)
    return lines
