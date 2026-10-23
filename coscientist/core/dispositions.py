"""Question dispositions (PR 2 not_attempted baseline; PR 8 wires verdict())."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coscientist.core.claims import (
    claims_by_target,
    claims_missing_about,
    disposition_from_verdict,
)
from coscientist.core.domain import Study, Task, TaskState
from coscientist.core.graph import GraphStore
from coscientist.core.question_tasks import known_questions, question_tasks
from coscientist.core.reasons import Disposition, NotAttemptedReason, TaskReason
from coscientist.core.rules import verdict


def _task_by_key(tasks: Mapping[str, Task]) -> dict[str, Task]:
    return {t.spec.task_key: t for t in tasks.values()}


def _reasons_for(tasks: list[Task]) -> list[TaskReason]:
    out: list[TaskReason] = []
    for t in tasks:
        if t.reason is None:
            continue
        if isinstance(t.reason, TaskReason):
            r = t.reason
        else:
            try:
                r = TaskReason(str(t.reason))
            except ValueError:
                continue
        if r not in out:
            out.append(r)
    return out


def not_attempted_reason(covering_outcomes: list[str | None]) -> str:
    """Map covering task outcomes → NotAttemptedReason value (oracle API)."""
    if not covering_outcomes:
        return NotAttemptedReason.NO_TASK.value
    done = [o for o in covering_outcomes if o is not None]
    if not done:
        return NotAttemptedReason.NO_TASK.value
    if any(o in ("SUCCESS", "PARTIAL") for o in done):
        return NotAttemptedReason.NO_COUNTING_CLAIMS.value
    if all(o == "FAILED" for o in done):
        return NotAttemptedReason.TASKS_FAILED.value
    return NotAttemptedReason.TASKS_SKIPPED.value


def _not_attempted_for_covering(
    covering: list[Task],
) -> tuple[NotAttemptedReason, list[TaskReason]]:
    if not covering:
        return NotAttemptedReason.NO_TASK, []
    done = [t for t in covering if t.state == TaskState.DONE]
    if not done:
        return NotAttemptedReason.NO_TASK, _reasons_for(covering)
    outcomes = [t.outcome.value if t.outcome is not None else None for t in done]
    return NotAttemptedReason(not_attempted_reason(outcomes)), _reasons_for(done)


def dispositions(
    *,
    tasks: Mapping[str, Task],
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
    graph: GraphStore | None = None,
    study: Study | None = None,
    results: Mapping[str, Mapping[str, Any]] | None = None,
    k: int = 1,
) -> dict[str, dict[str, Any]]:
    """Per-question disposition.

    With graph claims (PR 8): counting claims → ``verdict()`` → answered /
    refuted / survived / inconclusive (Study ``refutations_needed`` /
    ``refutation_priority``). Otherwise not_attempted + NotAttemptedReason.

    Claims missing the required ``about`` edge are ignored for counting
    (cardinality checked via ``claims_missing_about``).
    """
    qmap = known_questions(plans)
    cover = question_tasks(plans)
    by_key = _task_by_key(tasks)
    by_target: dict[str, list] = {}
    missing_about: set[str] = set()
    if graph is not None and study is not None:
        missing_about = set(claims_missing_about(graph))
        by_target = claims_by_target(
            graph, study_id=study.study_id, results=results
        )
        # Drop claims that violate about cardinality from counting sets.
        if missing_about:
            for ref, clist in list(by_target.items()):
                by_target[ref] = [c for c in clist if c.node_id not in missing_about]

    k_ref = study.refutations_needed if study is not None else 1
    priority = study.refutation_priority if study is not None else False

    result: dict[str, dict[str, Any]] = {}
    for qid, text in qmap.items():
        keys = cover.get(qid, set())
        covering = [by_key[k] for k in sorted(keys) if k in by_key]
        claims = by_target.get(qid) or []
        counting = [c for c in claims if c.counts()]
        if counting:
            any_exp = any(t.state == TaskState.DONE for t in covering) or True
            v, _by_lit = verdict(
                claims,
                any_exp,
                k,
                refutations_needed=k_ref,
                refutation_priority=priority,
            )
            disp = disposition_from_verdict(v)
            if disp is not None:
                result[qid] = {
                    "disposition": disp,
                    "reason": None,
                    "reason_detail": [],
                    "formulation": text,
                    "verdict": v,
                }
                continue
        reason, detail = _not_attempted_for_covering(covering)
        result[qid] = {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": reason.value,
            "reason_detail": [r.value for r in detail],
            "formulation": text,
        }
    return result
