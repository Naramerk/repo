"""Question dispositions (PR 2 not_attempted baseline; PR 8 wires verdict())."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coscientist.core.claims import (
    disposition_from_verdict,
    filtered_claims_by_target,
    hypothesis_verdicts,
)
from coscientist.core.domain import Study, Task, TaskOutcome, TaskState
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
    k: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-question disposition.

    With graph claims (PR 8): counting claims → ``verdict()`` → answered /
    refuted / survived / inconclusive (Study ``refutations_needed`` /
    ``refutation_priority``). Otherwise not_attempted + NotAttemptedReason.

    Totality: never raises — graph/claim coerce failures fall back to
    not_attempted.
    """
    try:
        return _dispositions_inner(
            tasks=tasks,
            plans=plans,
            graph=graph,
            study=study,
            results=results,
            k=k,
        )
    except Exception:  # noqa: BLE001 — finalize/view must never crash
        qmap = known_questions(plans)
        return {
            qid: {
                "disposition": Disposition.NOT_ATTEMPTED.value,
                "reason": NotAttemptedReason.NO_COUNTING_CLAIMS.value,
                "reason_detail": [],
                "formulation": text,
            }
            for qid, text in qmap.items()
        }


def _dispositions_inner(
    *,
    tasks: Mapping[str, Task],
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
    graph: GraphStore | None,
    study: Study | None,
    results: Mapping[str, Mapping[str, Any]] | None,
    k: int | None,
) -> dict[str, dict[str, Any]]:
    qmap = known_questions(plans)
    cover = question_tasks(plans)
    by_key = _task_by_key(tasks)
    by_target: dict[str, list] = {}
    if graph is not None and study is not None:
        by_target = filtered_claims_by_target(
            graph, study_id=study.study_id, results=results
        )

    k_ref = study.refutations_needed if study is not None else 1
    priority = study.refutation_priority if study is not None else False
    conf_k = (
        k
        if k is not None
        else (study.confirmations_needed if study is not None else 1)
    )

    result: dict[str, dict[str, Any]] = {}
    for qid, text in qmap.items():
        keys = cover.get(qid, set())
        covering = [by_key[kk] for kk in sorted(keys) if kk in by_key]
        claims = by_target.get(qid) or []
        counting = [c for c in claims if c.counts()]
        if counting:
            any_exp = any(t.state == TaskState.DONE for t in covering)
            v, _by_lit = verdict(
                claims,
                any_exp,
                conf_k,
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


def report_hypothesis_verdicts(
    *,
    graph: GraphStore | None,
    study: Study | None,
    results: Mapping[str, Mapping[str, Any]] | None,
    tasks: Mapping[str, Task],
) -> dict[str, dict[str, Any]]:
    """Hypothesis verdicts for StudyFinalized / minimal_report (PR8)."""
    if graph is None or study is None:
        return {}
    any_exp = any(
        t.state == TaskState.DONE
        and t.outcome in (TaskOutcome.SUCCESS, TaskOutcome.PARTIAL, TaskOutcome.FAILED)
        for t in tasks.values()
    )
    try:
        return hypothesis_verdicts(
            graph,
            study_id=study.study_id,
            study=study,
            results=results,
            k=1,
            any_experiment_done=any_exp,
        )
    except Exception:  # noqa: BLE001
        return {}
