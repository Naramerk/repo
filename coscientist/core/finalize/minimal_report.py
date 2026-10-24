"""Deterministic minimal_report builder (≤ 10 KiB after JCS).

R16a: typed ``MinimalReport`` is the public shape; MD takes that model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from coscientist.core.claims.reasons import Disposition, TaskReason
from coscientist.core.events.digest import canonical_json
from coscientist.core.execution.domain import (
    VALID_PROVENANCE,
    Provenance,
    Task,
    TaskOutcome,
    TaskState,
)
from coscientist.core.finalize.minimal_report_models import (
    MinimalReport,
    MinimalReportRefuse,
    MinimalReportSections,
)

MAX_BYTES = 10_240


def _task_provenance(
    task: Task, attempts: Mapping[str, Mapping[str, Any]]
) -> Provenance:
    for aid in reversed(task.attempts):
        if aid not in attempts:
            continue
        att = attempts[aid]
        if not att.get("closed"):
            continue
        if "provenance" not in att:
            raise MinimalReportRefuse(
                f"task {task.spec.task_key!r}: closed attempt provenance required"
            )
        prov = str(att["provenance"])
        if prov not in VALID_PROVENANCE:
            raise MinimalReportRefuse(
                f"task {task.spec.task_key!r}: unknown provenance {prov!r}"
            )
        return prov  # type: ignore[return-value]
    return "real"


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
    k = sum(
        1
        for q in questions
        if q.get("disposition") in (Disposition.NOT_ATTEMPTED, "not_attempted")
    )
    return (
        f"Study закрыто: {terminal_reason}; задач {n} "
        f"(SUCCESS {counts['SUCCESS']}, PARTIAL {counts['PARTIAL']}, "
        f"FAILED {counts['FAILED']}, SKIPPED {counts['SKIPPED']}); "
        f"вопросов {m}, not_attempted {k}"
    )


def _sections(
    tasks: list[Task], attempts: Mapping[str, Mapping[str, Any]]
) -> MinimalReportSections:
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
    return MinimalReportSections(real=real, stub=stub, human=human)


def assemble_minimal_report(
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
) -> MinimalReport:
    """Full (non-truncated) typed minimal report."""
    task_list = sorted(tasks.values(), key=lambda t: t.spec.task_key)
    q_rows: list[dict[str, Any]] = []
    for qid, d in sorted(question_dispositions.items()):
        formulation = d.get("formulation")
        disposition = d.get("disposition")
        reason = d.get("reason")
        if formulation is None or disposition is None or reason is None:
            raise MinimalReportRefuse(
                f"question {qid!r}: formulation/disposition/reason required"
            )
        if isinstance(disposition, Disposition):
            disp = disposition
        else:
            try:
                disp = Disposition(str(disposition))
            except ValueError as e:
                raise MinimalReportRefuse(
                    f"question {qid!r}: unknown disposition {disposition!r}"
                ) from e
        note_raw = d.get("note")
        row: dict[str, Any] = {
            "question_id": str(qid),
            "formulation": str(formulation)[:500],
            "disposition": disp,
            "reason": str(reason)[:200],
            "note": None if note_raw is None else str(note_raw),
        }
        q_rows.append(row)
    if isinstance(hypotheses, Mapping):
        h_rows: list[dict[str, Any]] = []
        for hid, v in sorted(hypotheses.items()):
            if not isinstance(v, Mapping):
                raise MinimalReportRefuse(
                    f"hypothesis {hid!r}: expected mapping row, got {type(v).__name__}"
                )
            row_h = dict(v)
            rid = row_h.get("hypothesis_id")
            if rid is None:
                raise MinimalReportRefuse(
                    f"hypothesis {hid!r}: hypothesis_id required in row"
                )
            if str(rid) != str(hid):
                raise MinimalReportRefuse(
                    f"hypothesis key {hid!r} != row hypothesis_id {rid!r}"
                )
            h_rows.append(row_h)
    elif hypotheses is None:
        h_rows = []
    else:
        h_rows = [dict(h) for h in hypotheses]
    t_rows: list[dict[str, Any]] = []
    for t in task_list:
        if t.state != TaskState.DONE and t.outcome is None:
            continue
        if t.outcome is None:
            raise MinimalReportRefuse(
                f"task {t.spec.task_key!r}: outcome required"
            )
        reason_s: str | None
        if isinstance(t.reason, TaskReason):
            reason_s = t.reason.value
        elif t.reason:
            reason_s = str(t.reason)
        else:
            reason_s = None
        t_rows.append(
            {
                "task_key": t.spec.task_key,
                "kind": t.kind.value,
                "outcome": t.outcome,
                "reason": reason_s,
                "provenance": _task_provenance(t, attempts),
            }
        )
    q_n = len(q_rows)
    na_n = sum(
        1 for q in q_rows if q["disposition"] is Disposition.NOT_ATTEMPTED
    )
    if custom_checks is None:
        checks: list[dict[str, Any]] = []
    else:
        checks = [dict(c) for c in custom_checks]
    obs_list: list[Any] = []
    if observations is not None:
        for obs in observations:
            if isinstance(obs, Mapping):
                obs_list.append(dict(obs))
            else:
                obs_list.append(obs)
    raw: dict[str, Any] = {
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
        "sections": _sections(task_list, attempts).model_dump(mode="json"),
        "custom_checks": checks,
        "observations": obs_list,
        "truncated": False,
    }
    try:
        return MinimalReport.model_validate(raw)
    except ValidationError as e:
        raise MinimalReportRefuse(f"MinimalReport: {e}") from e


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
) -> MinimalReport:
    return truncate_minimal_report(
        assemble_minimal_report(
            study_id=study_id,
            terminal_reason=terminal_reason,
            trigger=trigger,
            contributing_reasons=contributing_reasons,
            verdict=verdict,
            tasks=tasks,
            attempts=attempts,
            question_dispositions=question_dispositions,
            fallback_reason=fallback_reason,
            refusal=refusal,
            hypotheses=hypotheses,
            hypotheses_unavailable=hypotheses_unavailable,
            custom_checks=custom_checks,
            observations=observations,
        )
    )


def _jcs_size(report: MinimalReport) -> int:
    return len(canonical_json(report.model_dump(mode="json")))


def truncate_minimal_report(report: MinimalReport) -> MinimalReport:
    """Shrink lists until JCS size ≤ MAX_BYTES; summary counters stay intact."""
    out = report.model_copy(update={"truncated": False})
    if _jcs_size(out) <= MAX_BYTES:
        return out
    out = out.model_copy(update={"truncated": True})
    qs = [
        q.model_copy(update={"formulation": q.formulation[:80]}) for q in out.questions
    ]
    out = out.model_copy(update={"questions": qs})
    if _jcs_size(out) <= MAX_BYTES:
        return out
    while _jcs_size(out) > MAX_BYTES and out.tasks:
        out = out.model_copy(update={"tasks": list(out.tasks[:-1])})
    while _jcs_size(out) > MAX_BYTES and out.questions:
        out = out.model_copy(update={"questions": list(out.questions[:-1])})
    while _jcs_size(out) > MAX_BYTES and out.hypotheses:
        out = out.model_copy(update={"hypotheses": list(out.hypotheses[:-1])})
    while _jcs_size(out) > MAX_BYTES and out.observations:
        out = out.model_copy(update={"observations": list(out.observations[:-1])})
    while _jcs_size(out) > MAX_BYTES and out.custom_checks:
        out = out.model_copy(update={"custom_checks": list(out.custom_checks[:-1])})
    secs = out.sections
    for key in ("human", "stub", "real"):
        while _jcs_size(out) > MAX_BYTES and getattr(secs, key):
            cur = list(getattr(secs, key))
            secs = secs.model_copy(update={key: cur[:-1]})
            out = out.model_copy(update={"sections": secs})
    return out


def render_markdown_report(
    report: MinimalReport,
    *,
    as_of: str | None = None,
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]] | None = None,
    tasks: Mapping[str, Task] | None = None,
) -> str:
    """Deterministic MD report from typed ``MinimalReport`` (no LLM)."""
    study_id = report.study_id
    terminal_reason = report.terminal_reason
    trigger = report.trigger
    verdict = report.verdict
    contributing_reasons = list(report.contributing_reasons)
    refusal = report.refusal
    question_dispositions = {
        q.question_id: {
            "disposition": q.disposition.value,
            "reason": q.reason,
            "formulation": q.formulation,
            "note": q.note,
        }
        for q in report.questions
    }
    custom_checks = list(report.custom_checks)
    observations: list[Any] = list(report.observations)
    if tasks is None:
        task_map: Mapping[str, Task] = {}
    else:
        task_map = tasks
    lines = [
        f"# Study report `{study_id}`",
        "",
        f"- trigger: `{trigger}`",
        f"- terminal_reason: `{terminal_reason}`",
        f"- verdict: `{verdict}`",
        (
            f"- contributing_reasons: {', '.join(contributing_reasons)}"
            if contributing_reasons
            else "- contributing_reasons: (none)"
        ),
    ]
    if as_of:
        lines.append(f"- as_of: `{as_of}`")
    if refusal:
        lines.append(f"- refusal: `{refusal}`")
    not_done = [c for c in contributing_reasons if str(c).startswith("не выполнено:")]
    for line in not_done:
        lines.append(f"- {line}")
    lines.extend(["", "## Questions"])
    if not report.questions:
        lines.append("_No questions._")
    for q in report.questions:
        extra = f"; note={q.note}" if q.note is not None else ""
        lines.append(
            f"- `{q.question_id}`: {q.disposition.value} ({q.reason}) — "
            f"{q.formulation}{extra}"
        )
    lines.extend(["", "## Tasks"])
    # Single path: Tasks section always from typed MinimalReport rows.
    for row in report.tasks:
        lines.append(
            f"- `{row.task_key}` [{row.kind}] {row.outcome.value} "
            f"reason={row.reason} provenance={row.provenance}"
        )
    tree = render_subtask_tree(
        question_dispositions=question_dispositions,
        tasks=task_map,
        plans=plans,
    )
    if tree:
        lines.extend(["", "## Дерево подзадач", *tree])
    lines.extend(["", "## Custom checks"])
    if not custom_checks:
        lines.append("_None._")
    else:
        from coscientist.core.finalize.finalize import _escape_report_text

        for c in custom_checks:
            who = c.get("approved_by") or c.get("rejected_by") or "?"
            delg = "delegated" if c.get("delegated") else "human"
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
        origin = origins.get(qid, "explicit")
        rev = revs.get(qid, 1)
        disp = d.get("disposition", "?")
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
