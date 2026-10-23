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
from coscientist.core.rubric import (
    RUBRIC_REJECTED_LINE,
    RUBRIC_REJECTED_REASON,
    question_final_types,
    task_rubric_status,
)
from coscientist.core.rules import priority_refuted_support_note
from coscientist.core.verdict import NO_EFFECT_EXCLUSION_REASONS


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


# Honest terminals for PR16 rollup clamp (``verdict is not None``). Explicit
# needs_more_data / infeasible (PR15) and computed ill_posed (PR8) must stamp
# ``verdict`` so incomplete children cannot wipe the parent.
_HONEST_TERMINAL_DISPOSITIONS: frozenset[str] = frozenset(
    {
        Disposition.NEEDS_MORE_DATA.value,
        Disposition.INFEASIBLE.value,
        Disposition.ILL_POSED.value,
    }
)


def stamp_honest_terminal_verdict(row: dict[str, Any]) -> dict[str, Any]:
    """Ensure needs_more_data / infeasible / ill_posed carry a terminal verdict.

    PR16 ``_has_own_terminal_verdict`` is true when ``disposition != not_attempted``
    **or** ``verdict is not None``. Stamping ``verdict`` keeps the contract
    explicit for claims-style clamp paths. not_attempted fallbacks omit it.
    """
    disp = str(row.get("disposition") or "")
    if disp in _HONEST_TERMINAL_DISPOSITIONS and row.get("verdict") is None:
        row["verdict"] = disp
    return row


def _stamp_honest_terminals(result: dict[str, dict[str, Any]]) -> None:
    for row in result.values():
        stamp_honest_terminal_verdict(row)


def dispositions(
    *,
    tasks: Mapping[str, Task],
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
    graph: GraphStore | None = None,
    study: Study | None = None,
    results: Mapping[str, Mapping[str, Any]] | None = None,
    k: int | None = None,
    artifact_lookup: Any | None = None,
    loop_capped_questions: Mapping[str, Any] | None = None,
    question_disposals: Mapping[str, Mapping[str, Any]] | None = None,
    journal_kinds: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-question disposition.

    With graph claims (PR 8): counting claims → ``verdict()`` → answered /
    refuted / survived / inconclusive (Study ``refutations_needed`` /
    ``refutation_priority``). Else explicit ``QuestionDisposed``
    (needs_more_data|infeasible; PR15). Otherwise not_attempted.

    PR11: questions in ``loop_capped_questions`` keep a verdict disposition
    when available and set ``reason=iteration_cap`` (not study terminal_reason).

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
            artifact_lookup=artifact_lookup,
            loop_capped_questions=loop_capped_questions,
            question_disposals=question_disposals,
            journal_kinds=journal_kinds,
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
    artifact_lookup: Any | None = None,
    loop_capped_questions: Mapping[str, Any] | None = None,
    question_disposals: Mapping[str, Mapping[str, Any]] | None = None,
    journal_kinds: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    qmap = known_questions(plans)
    cover = question_tasks(plans)
    by_key = _task_by_key(tasks)
    by_target: dict[str, list] = {}
    if graph is not None and study is not None:
        by_target = filtered_claims_by_target(
            graph,
            study_id=study.study_id,
            results=results,
            artifact_lookup=artifact_lookup,
        )

    k_ref = study.refutations_needed if study is not None else 1
    priority = study.refutation_priority if study is not None else False
    conf_k = k if k is not None else (study.confirmations_needed if study is not None else 1)
    supp_k = study.supported_needed if study is not None else 2

    final_types = question_final_types(plans)
    result: dict[str, dict[str, Any]] = {}
    for qid, text in qmap.items():
        keys = cover.get(qid, set())
        covering = [by_key[kk] for kk in sorted(keys) if kk in by_key]
        ft = final_types.get(qid, "verdict")
        claims = claims_for_ref(by_target, qid, prefer="question")  # explicit kind
        counting = [c for c in claims if c.counts()]
        excluded = excluded_claims_report(claims)
        validity_excl = [e for e in excluded if e["reason"] == "validity_failed"]
        any_exp = any(t.state == TaskState.DONE for t in covering)
        # PR13: rubric status for artifact / both (§6.1). Errors propagate
        # to the study-level dispositions() fallback (honest internal error).
        rub_st: str | None = None
        if ft in ("artifact", "both"):
            rub_st = task_rubric_status(task_keys=keys, tasks=tasks, results=results)
        if ft == "artifact":
            if rub_st == "met":
                result[qid] = {
                    "disposition": Disposition.ANSWERED.value,
                    "reason": "rubric_met",
                    "reason_detail": [],
                    "formulation": text,
                    "final_type": ft,
                    "verdict": "answered",
                    "report_line": None,
                }
                continue
            if rub_st == "failed":
                result[qid] = {
                    "disposition": Disposition.RUBRIC_NOT_MET.value,
                    "reason": RUBRIC_REJECTED_REASON,
                    "reason_detail": [RUBRIC_REJECTED_REASON],
                    "formulation": text,
                    "final_type": ft,
                    "verdict": Disposition.RUBRIC_NOT_MET.value,
                    "report_line": RUBRIC_REJECTED_LINE,
                }
                continue
            # PR15: needs_more_data is an honest artifact-path final (design report).
            explicit_art = (question_disposals or {}).get(qid)
            if explicit_art and str(explicit_art.get("disposition") or "") == (
                Disposition.NEEDS_MORE_DATA.value
            ):
                from coscientist.core.experiment_design import (
                    MISSING_DESIGN_REASON,
                    design_artifact_ok,
                )

                if design_artifact_ok(
                    artifact_id=explicit_art.get("design_artifact_id"),
                    artifact_lookup=artifact_lookup,
                    tasks=tasks,
                    results=results,
                    task_keys=keys,
                    journal_kinds=dict(journal_kinds) if journal_kinds else None,
                ):
                    result[qid] = {
                        "disposition": Disposition.NEEDS_MORE_DATA.value,
                        "reason": str(explicit_art.get("reason") or "needs_more_data"),
                        "reason_detail": list(explicit_art.get("refs") or []),
                        "formulation": text,
                        "final_type": ft,
                        "verdict": Disposition.NEEDS_MORE_DATA.value,
                        "design_artifact_id": explicit_art.get("design_artifact_id"),
                    }
                    continue
                reason, detail = _not_attempted_for_covering(covering)
                result[qid] = {
                    "disposition": Disposition.NOT_ATTEMPTED.value,
                    "reason": MISSING_DESIGN_REASON,
                    "reason_detail": [r.value for r in detail] + [MISSING_DESIGN_REASON],
                    "formulation": text,
                    "final_type": ft,
                }
                continue
            reason, detail = _not_attempted_for_covering(covering)
            result[qid] = {
                "disposition": Disposition.NOT_ATTEMPTED.value,
                "reason": reason.value,
                "reason_detail": [r.value for r in detail],
                "formulation": text,
                "final_type": ft,
            }
            continue
        if counting:
            # Per-experiment exclusion: verdict from valid claims only.
            from coscientist.core.derivation import by_theory_mark
            from coscientist.core.verdict import disposition_for_question, hypothesis_verdict

            v, _by_lit, by_theory, _rule = hypothesis_verdict(
                claims,
                any_exp,
                refutations_needed=k_ref,
                refutation_priority=priority,
                supported_needed=supp_k,
                confirmations_needed=conf_k,
            )
            if not by_theory:
                by_theory = by_theory_mark(claims)
            disp = disposition_for_question(
                hypothesis_verdicts={},
                question_verdict=v,
                has_hypotheses=False,
            )
            if disp != "not_attempted":
                # §6.1 both: verdict AND rubric required.
                if ft == "both":
                    if rub_st != "met":
                        if rub_st == "failed":
                            result[qid] = {
                                "disposition": Disposition.RUBRIC_NOT_MET.value,
                                "reason": RUBRIC_REJECTED_REASON,
                                "reason_detail": [RUBRIC_REJECTED_REASON],
                                "formulation": text,
                                "final_type": ft,
                                "verdict": v,
                                "report_line": RUBRIC_REJECTED_LINE,
                            }
                        else:
                            # Verdict present, rubric still pending — own reason.
                            result[qid] = {
                                "disposition": Disposition.NOT_ATTEMPTED.value,
                                "reason": "rubric_pending",
                                "reason_detail": ["rubric_pending"],
                                "formulation": text,
                                "final_type": ft,
                                "verdict": v,
                                "report_line": None,
                            }
                        if excluded:
                            result[qid]["excluded_claims"] = excluded
                        continue
                row: dict[str, Any] = {
                    "disposition": disp,
                    "reason": None,
                    "reason_detail": [],
                    "formulation": text,
                    "verdict": v,
                    "final_type": ft,
                    "by_theory": by_theory,
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
        elif ft == "both" and rub_st == "met" and not counting:
            # Rubric alone is not enough for both.
            reason, detail = _not_attempted_for_covering(covering)
            result[qid] = {
                "disposition": Disposition.NOT_ATTEMPTED.value,
                "reason": reason.value,
                "reason_detail": [r.value for r in detail],
                "formulation": text,
                "final_type": ft,
            }
            continue
        elif ft == "both" and rub_st == "failed":
            result[qid] = {
                "disposition": Disposition.RUBRIC_NOT_MET.value,
                "reason": RUBRIC_REJECTED_REASON,
                "reason_detail": [RUBRIC_REJECTED_REASON],
                "formulation": text,
                "final_type": ft,
                "verdict": Disposition.RUBRIC_NOT_MET.value,
                "report_line": RUBRIC_REJECTED_LINE,
            }
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
        # No-effect indeterminate (foreign / missing output / empty pointer):
        # inconclusive with the specific reason — never generic "unclear".
        no_effect_excl = [e for e in excluded if e.get("reason") in NO_EFFECT_EXCLUSION_REASONS]
        if no_effect_excl:
            reasons = sorted({str(e["reason"]) for e in no_effect_excl})
            primary = reasons[0]
            result[qid] = {
                "disposition": Disposition.INCONCLUSIVE.value,
                "reason": primary,
                "reason_detail": reasons,
                "formulation": text,
                "verdict": "inconclusive",
                "excluded_claims": excluded,
            }
            continue
        # PR15 / §2: explicit QuestionDisposed when no counting claims.
        explicit = (question_disposals or {}).get(qid)
        if explicit and str(explicit.get("disposition") or "") in (
            Disposition.NEEDS_MORE_DATA.value,
            Disposition.INFEASIBLE.value,
        ):
            disp = str(explicit["disposition"])
            if disp == Disposition.NEEDS_MORE_DATA.value:
                from coscientist.core.experiment_design import (
                    MISSING_DESIGN_REASON,
                    design_artifact_ok,
                )

                if not design_artifact_ok(
                    artifact_id=explicit.get("design_artifact_id"),
                    artifact_lookup=artifact_lookup,
                    tasks=tasks,
                    results=results,
                    task_keys=keys,
                    journal_kinds=dict(journal_kinds) if journal_kinds else None,
                ):
                    # Honest: disposal without design does not stick.
                    reason, detail = _not_attempted_for_covering(covering)
                    result[qid] = {
                        "disposition": Disposition.NOT_ATTEMPTED.value,
                        "reason": MISSING_DESIGN_REASON,
                        "reason_detail": [r.value for r in detail] + [MISSING_DESIGN_REASON],
                        "formulation": text,
                    }
                    if excluded:
                        result[qid]["excluded_claims"] = excluded
                    continue
            row_ex: dict[str, Any] = {
                "disposition": disp,
                "reason": str(explicit.get("reason") or disp),
                "reason_detail": list(explicit.get("refs") or []),
                "formulation": text,
                "verdict": disp,
            }
            if disp == Disposition.NEEDS_MORE_DATA.value:
                row_ex["design_artifact_id"] = explicit.get("design_artifact_id")
            if excluded:
                row_ex["excluded_claims"] = excluded
            result[qid] = row_ex
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

    # PR11: iteration_cap on unresolved loop-scoped questions (before rollup).
    apply_loop_cap_dispositions(result, loop_capped_questions)
    # Stamp honest terminals before rollup so clamp sees verdict.
    _stamp_honest_terminals(result)
    # PR16: parent disposition from frozen Plan.rollup (DESIGN_M1 §10).
    from coscientist.core.rollup import apply_rollup

    return apply_rollup(result, plans=plans)


def apply_loop_cap_dispositions(
    result: dict[str, dict[str, Any]],
    loop_capped_questions: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Tag only questions genuinely unresolved by an iteration_cap.

    - Refuse/error LoopCapped payloads must not appear in ``loop_capped_questions``
      (caller filters via ``capped_scope_questions``).
    - Answered / refuted / survived (including from tasks outside the loop) keep
      their disposition and reason — never overwritten with iteration_cap.
    - infeasible / needs_more_data / ill_posed preserved.
    - not_attempted → inconclusive + iteration_cap.
    - Post-#21: ``TerminalReason.INTERNAL_ERROR`` must not be conflated with
      iteration_cap here (error/refuse stays honest).
    """
    capped_q = {str(k): v for k, v in (loop_capped_questions or {}).items()}
    if not capped_q:
        return result
    answered = (
        Disposition.ANSWERED.value,
        Disposition.REFUTED.value,
        Disposition.SURVIVED.value,
        Disposition.INCONCLUSIVE.value,
    )
    preserve = (
        Disposition.INFEASIBLE.value,
        Disposition.NEEDS_MORE_DATA.value,
        Disposition.ILL_POSED.value,
    )
    for qid, row in list(result.items()):
        if qid not in capped_q:
            continue
        p = capped_q[qid]
        if isinstance(p, dict):
            reason = str(p.get("reason") or TaskReason.ITERATION_CAP.value)
            if reason != TaskReason.ITERATION_CAP.value:
                continue
            try:
                if int(p.get("iter") or 0) < 1:
                    continue
            except (TypeError, ValueError):
                continue
        disp = str(row.get("disposition") or "")
        if disp in preserve:
            continue
        if disp in answered:
            # Real answer (possibly from outside-loop tasks) — leave untouched.
            continue
        # not_attempted / empty: unresolved because the loop hit the cap.
        row = dict(row)
        row["disposition"] = Disposition.INCONCLUSIVE.value
        row["reason"] = TaskReason.ITERATION_CAP.value
        row["reason_detail"] = [TaskReason.ITERATION_CAP.value]
        result[qid] = row
    return result


def report_hypothesis_verdicts(
    *,
    graph: GraphStore | None,
    study: Study | None,
    results: Mapping[str, Mapping[str, Any]] | None,
    tasks: Mapping[str, Task],
    artifact_lookup: Any | None = None,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Hypothesis verdicts for StudyFinalized / minimal_report (PR8/PR8b).

    Returns ``(rows, hypotheses_unavailable)``. On failure ``rows`` is empty and
    ``hypotheses_unavailable`` is the exception class name — never a fake
    hypothesis id.
    """
    if graph is None or study is None:
        return {}, None
    any_exp = any(
        t.state == TaskState.DONE and t.outcome in (TaskOutcome.SUCCESS, TaskOutcome.PARTIAL, TaskOutcome.FAILED)
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
            graph,
            study_id=study.study_id,
            results=results,
            artifact_lookup=artifact_lookup,
        )
        out: dict[str, dict[str, Any]] = {}
        for hid, snap in snaps.items():
            claims = claims_for_ref(by_target, hid, prefer="hypothesis")
            v = str(snap.get("verdict") or "untested")
            _v, _b, _by_th, rule = hypothesis_verdict(
                claims,
                any_exp,
                refutations_needed=study.refutations_needed,
                refutation_priority=study.refutation_priority,
                supported_needed=study.supported_needed,
                confirmations_needed=study.confirmations_needed,
            )
            legacy = sum(1 for c in claims if getattr(c, "severe", None) is False and c.claim_outcome == "consistent")
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
                    "by_theory": snap.get("by_theory", False),
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
