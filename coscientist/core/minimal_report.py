"""Deterministic minimal_report builder (≤ 10 KiB after JCS)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coscientist.core.digest import canonical_json
from coscientist.core.domain import Task, TaskOutcome, TaskState
from coscientist.core.reasons import TaskReason

MAX_BYTES = 10_240


def _task_provenance(task: Task, attempts: Mapping[str, Mapping[str, Any]]) -> str:
    for aid in reversed(task.attempts):
        att = attempts.get(aid, {})
        if att.get("closed"):
            return str(att.get("provenance", "real"))
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
) -> dict[str, Any]:
    task_list = sorted(tasks.values(), key=lambda t: t.spec.task_key)
    q_rows = [
        {
            "question_id": qid,
            "formulation": str(d.get("formulation", ""))[:500],
            "disposition": str(d.get("disposition", "not_attempted")),
            "reason": str(d.get("reason", ""))[:200],
        }
        for qid, d in sorted(question_dispositions.items())
    ]
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
    report: dict[str, Any] = {
        "study_id": study_id,
        "terminal_reason": terminal_reason,
        "trigger": trigger,
        "contributing_reasons": contributing_reasons[:20],
        "summary": _summary(terminal_reason, task_list, q_rows),
        "fallback_reason": fallback_reason,
        "refusal": None,
        "verdict": verdict,
        "tasks": t_rows,
        "questions": q_rows,
        "sections": _sections(task_list, attempts),
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
) -> str:
    """Deterministic MD report (no LLM)."""
    lines = [
        f"# Study report `{study_id}`",
        "",
        f"- trigger: `{trigger}`",
        f"- terminal_reason: `{terminal_reason}`",
        f"- verdict: `{verdict}`",
        f"- contributing_reasons: {', '.join(contributing_reasons) or '(none)'}",
        "",
        "## Questions",
    ]
    if not question_dispositions:
        lines.append("_No questions._")
    for qid, d in sorted(question_dispositions.items()):
        lines.append(
            f"- `{qid}`: {d.get('disposition')} ({d.get('reason')}) — {d.get('formulation', '')}"
        )
    lines.extend(["", "## Tasks"])
    for t in sorted(tasks.values(), key=lambda x: x.spec.task_key):
        lines.append(
            f"- `{t.spec.task_key}` [{t.kind.value}] "
            f"{t.state.value}/{t.outcome} reason={t.reason}"
        )
    return "\n".join(lines) + "\n"
