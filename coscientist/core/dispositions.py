"""Question dispositions (PR 2 not_attempted baseline; PR 8 wires verdict())."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coscientist.core.claims import (
    claims_for_ref,
    excluded_claims_report,
    filtered_claims_by_target,
    hypothesis_verdicts,
)
from coscientist.core.domain import Study, Task, TaskOutcome, TaskState
from coscientist.core.graph import GraphStore
from coscientist.core.question_tasks import known_questions, question_tasks
from coscientist.core.reasons import Disposition, NotAttemptedReason, TaskReason
from coscientist.core.rules import priority_refuted_support_note, verdict


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
    supp_k = study.supported_needed if study is not None else 2

    result: dict[str, dict[str, Any]] = {}
    for qid, text in qmap.items():
        keys = cover.get(qid, set())
        covering = [by_key[kk] for kk in sorted(keys) if kk in by_key]
        claims = claims_for_ref(by_target, qid, prefer="question")  # explicit kind
        counting = [c for c in claims if c.counts()]
        excluded = excluded_claims_report(claims)
        validity_excl = [e for e in excluded if e["reason"] == "validity_failed"]
        any_exp = any(t.state == TaskState.DONE for t in covering)
        if counting:
            # Per-experiment exclusion: verdict from valid claims only.
            v, _by_lit = verdict(
                claims,
                any_exp,
                conf_k,
                refutations_needed=k_ref,
                refutation_priority=priority,
                supported_needed=supp_k,
            )
            from coscientist.core.verdict import disposition_for_question

            disp = disposition_for_question(
                hypothesis_verdicts={},
                question_verdict=v,
                has_hypotheses=False,
            )
            if disp != "not_attempted":
                row: dict[str, Any] = {
                    "disposition": disp,
                    "reason": None,
                    "reason_detail": [],
                    "formulation": text,
                    "verdict": v,
                }
                note = priority_refuted_support_note(
                    claims,
                    any_exp,
                    conf_k,
                    refutations_needed=k_ref,
                    refutation_priority=priority,
                    supported_needed=supp_k,
                )
                if note is not None:
                    row["note"] = note
                if excluded:
                    row["excluded_claims"] = excluded
                result[qid] = row
                continue
        # No valid claims: validity_failed → inconclusive; unknown_role alone
        # never forces a verdict (falls through to not_attempted).
        if validity_excl:
            result[qid] = {
                "disposition": Disposition.INCONCLUSIVE.value,
                "reason": "validity_failed",
                "reason_detail": ["validity_failed"],
                "formulation": text,
                "verdict": "inconclusive",
                "excluded_claims": excluded,
            }
            continue
        reason, detail = _not_attempted_for_covering(covering)
        row_na: dict[str, Any] = {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": reason.value,
            "reason_detail": [r.value for r in detail],
            "formulation": text,
        }
        if excluded:
            row_na["excluded_claims"] = excluded
        result[qid] = row_na
    return result


def report_hypothesis_verdicts(
    *,
    graph: GraphStore | None,
    study: Study | None,
    results: Mapping[str, Mapping[str, Any]] | None,
    tasks: Mapping[str, Task],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Hypothesis verdicts for StudyFinalized / minimal_report (PR8/PR8b).

    Returns ``(rows, hypotheses_unavailable)``. On failure ``rows`` is empty and
    ``hypotheses_unavailable`` is the exception class name — never a fake
    hypothesis id.
    """
    if graph is None or study is None:
        return {}, None
    any_exp = any(
        t.state == TaskState.DONE
        and t.outcome in (TaskOutcome.SUCCESS, TaskOutcome.PARTIAL, TaskOutcome.FAILED)
        for t in tasks.values()
    )
    try:
        from coscientist.core.verdict import (
            hypothesis_report_row,
            hypothesis_verdict,
        )

        snaps = hypothesis_verdicts(
            graph,
            study_id=study.study_id,
            study=study,
            results=results,
            k=study.confirmations_needed,
            any_experiment_done=any_exp,
        )
        by_target = filtered_claims_by_target(
            graph, study_id=study.study_id, results=results
        )
        out: dict[str, dict[str, Any]] = {}
        for hid, snap in snaps.items():
            claims = claims_for_ref(by_target, hid, prefer="hypothesis")
            v = str(snap.get("verdict") or "untested")
            _v, _b, rule = hypothesis_verdict(
                claims,
                any_exp,
                refutations_needed=study.refutations_needed,
                refutation_priority=study.refutation_priority,
                supported_needed=study.supported_needed,
                confirmations_needed=study.confirmations_needed,
            )
            legacy = sum(
                1
                for c in claims
                if getattr(c, "severe", None) is False
                and c.claim_outcome == "consistent"
            )
            # question_id: first addresses edge from hypothesis, else ""
            qid = ""
            hid_node = f"{study.study_id}:hypothesis:{hid}"
            if graph.get_node(hid_node) is not None:
                for e in graph.edges_from(hid_node):
                    if e.kind == "addresses":
                        dst = graph.get_node(e.dst_id)
                        if dst is not None and dst.kind == "question":
                            qid = dst.ref_id or e.dst_id.split(":")[-1]
                            break
            row = hypothesis_report_row(
                hypothesis_id=hid,
                question_id=qid,
                claims=claims,
                verdict_value=v,
                rule=rule or snap.get("rule"),
                legacy_claims=legacy,
            )
            # Keep PR8 snapshot fields used by bind/digest consumers.
            row.update(
                {
                    "ref": snap.get("ref", hid),
                    "target_kind": "hypothesis",
                    "by_literature": snap.get("by_literature", False),
                    "provenance": snap.get("provenance", "real"),
                    "k": snap.get("k", study.confirmations_needed),
                    "supported_needed": study.supported_needed,
                    "refutations_needed": study.refutations_needed,
                    "refutation_priority": study.refutation_priority,
                }
            )
            if snap.get("note"):
                row["note"] = snap["note"]
            if snap.get("excluded_claims"):
                row.setdefault("excluded_claims", snap["excluded_claims"])
            out[hid] = row
        return out, None
    except Exception as exc:  # noqa: BLE001 — dedicated field, not a fake hypothesis
        return {}, type(exc).__name__
