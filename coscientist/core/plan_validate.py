"""PlanValidator for final_type + rubric (M1 PR13 / DESIGN_M1 §6.1).

- ``final_type=artifact`` → ≥1 addressing task with OutputSpec.rubric
- ``final_type=verdict`` → ≥1 addressing task with criterion (RequirementRef)
- ``final_type=both`` → both requirements

Also rejects in-place rubric digest changes on an already-frozen task_key
unless that key is superseded (change only via PlanExpanded / HITL).

Invalid plans → ``PlanRefused`` + immediate ``StudyFinalized`` (never raw
``ValueError`` escaping ``accept_plan``).

**Public API for #24 / callers:** ``apply_plan_refused``, ``refuse_detail``,
``PLAN_REFUSED_LINE`` — emit PlanRefused, close open tasks, finalize(trigger=intake).
"""

from __future__ import annotations

from typing import Any

from coscientist.core.domain import QuestionSpec, TaskSpec
from coscientist.core.rubric import freeze_rubric, freeze_task_rubrics
from coscientist.core.rubric_types import VALID_FINAL_TYPES, Rubric

__all__ = [
    "PLAN_REFUSED_LINE",
    "apply_plan_refused",
    "freeze_plan_rubrics",
    "refuse_detail",
    "validate_final_types",
    "validate_plan_final_types_or_err",
    "validate_rubric_immutable",
]


def _rubric_digest_of(spec: TaskSpec) -> str | None:
    digests: list[str] = []
    for o in spec.expected_outputs or []:
        rub = getattr(o, "rubric", None)
        if rub is None:
            continue
        if not isinstance(rub, Rubric):
            rub = Rubric.model_validate(rub)
        frozen = freeze_rubric(rub)
        digests.append(frozen.digest)
    if not digests:
        return None
    return "|".join(digests)


def _has_rubric(spec: TaskSpec) -> bool:
    return any(getattr(o, "rubric", None) is not None for o in (spec.expected_outputs or []))


def _has_criterion(spec: TaskSpec, catalog: Any | None = None) -> bool:
    """True iff the task declares ≥1 criterion. Catalog defaults do **not** count."""
    del catalog  # explicit criteria only — defaults are runtime eval, not plan coverage
    return bool(spec.criteria)


def validate_final_types(
    tasks: list[TaskSpec],
    questions: list[QuestionSpec],
    *,
    active_question_ids: set[str] | None = None,
    catalog: Any | None = None,
) -> None:
    """Raise ValueError if final_type coverage rules fail.

    When ``active_question_ids`` is set (expansion), only those questions are
    checked — disposed/superseded questions may be omitted (H-f / PR8).
    """
    q_by_id = {q.id: q for q in questions}
    cover: dict[str, list[TaskSpec]] = {qid: [] for qid in q_by_id}
    for spec in tasks:
        for qid in spec.addresses or []:
            if qid in cover:
                cover[qid].append(spec)

    check_ids = set(active_question_ids) if active_question_ids is not None else set(q_by_id)
    for qid in sorted(check_ids):
        q = q_by_id.get(qid)
        if q is None:
            continue
        ft = getattr(q, "final_type", None) or "verdict"
        if ft not in VALID_FINAL_TYPES:
            raise ValueError(f"question {qid!r}: unknown final_type {ft!r} (expected verdict|artifact|both)")
        covering = cover.get(qid) or []
        if not covering and ft in ("artifact", "both", "verdict"):
            # Unaddressed questions stay not_attempted; PlanValidator only
            # constrains questions that have tasks (H-f / partial coverage).
            continue
        if ft in ("artifact", "both"):
            if not any(_has_rubric(t) for t in covering):
                raise ValueError(f"question {qid!r} final_type={ft}: need OutputSpec with rubric on an addressing task")
        if ft in ("verdict", "both"):
            if not any(_has_criterion(t, catalog) for t in covering):
                raise ValueError(f"question {qid!r} final_type={ft}: need ≥1 criterion on an addressing task")


PLAN_REFUSED_LINE = "не выполнено: план отклонён (рубрика/final_type)"


def refuse_detail(err: BaseException | str, *, truncate: bool = False) -> str:
    """Full detail string for PlanRefused idem hashing.

    ``truncate=True`` only for human-facing report snippets — never before
    ``plan_refused_idem`` (idem must hash the full detail).
    """
    msg = str(err) if not isinstance(err, BaseException) else str(err)
    full = msg or type(err).__name__
    if truncate:
        return full[:200]
    return full


async def apply_plan_refused(
    runtime: Any,
    *,
    detail: str,
    report_line: str = PLAN_REFUSED_LINE,
) -> str:
    """Canonical plan refusal (PR13; #24 reuses — do not fork).

    Emits ``PlanRefused`` (idem = sid + sha256(full detail)), closes open tasks,
    ``finalize(trigger=\"intake\")`` → ``terminal_reason=REFUSED``. Returns ``\"\"``.
    """
    import coscientist.core.finalize as finalize_mod
    from coscientist.core.domain import TaskOutcome, TaskState
    from coscientist.core.events import EventEnvelope, make_idem
    from coscientist.core.reasons import TaskReason
    from coscientist.core.reviews import plan_refused_idem

    study = getattr(runtime, "study", None)
    if study is None:
        return ""
    if getattr(runtime, "_finalized", False):
        return ""
    sid = study.study_id
    now = runtime.clock.now()
    events: list[Any] = [
        EventEnvelope(
            study_id=sid,
            type="PlanRefused",
            entity_key=sid,
            idem_key=plan_refused_idem(sid, detail),
            payload={
                "reason": TaskReason.SCHEMA_INVALID.value,
                "detail": detail,
                "report_line": report_line,
            },
            ts=now,
            schema_version=1,
        )
    ]
    # Close PLAN + any still-open stage tasks so finalize is immediate.
    for t in list((getattr(runtime, "tasks", None) or {}).values()):
        if getattr(t, "state", None) == TaskState.DONE:
            continue
        events.append(
            EventEnvelope(
                study_id=sid,
                type="TaskDone",
                entity_key=t.task_id,
                idem_key=make_idem("TaskDone", t.task_id, "plan_refused"),
                payload={
                    "task_id": t.task_id,
                    "outcome": (
                        TaskOutcome.FAILED.value
                        if getattr(getattr(t, "spec", None), "task_key", None) == "PLAN"
                        else TaskOutcome.SKIPPED.value
                    ),
                    "reason": TaskReason.SCHEMA_INVALID.value,
                    "detail": detail,
                },
                ts=now,
                schema_version=2,
            )
        )
    await runtime._append(events)  # noqa: SLF001
    await finalize_mod.finalize(runtime, trigger="intake")
    return ""


def validate_rubric_immutable(
    *,
    new_tasks: list[TaskSpec],
    prev_tasks: list[TaskSpec],
    superseded_tasks: set[str] | None = None,
) -> None:
    """Rubric digest may change only when the old task_key is superseded."""
    skip = set(superseded_tasks or ())
    prev_digests: dict[str, str] = {}
    for t in prev_tasks:
        d = _rubric_digest_of(t)
        if d is not None:
            prev_digests[t.task_key] = d
    for t in new_tasks:
        if t.task_key in skip:
            continue
        new_d = _rubric_digest_of(t)
        old_d = prev_digests.get(t.task_key)
        if old_d is None or new_d is None:
            continue
        if new_d != old_d:
            raise ValueError(
                f"rubric change on {t.task_key!r} requires PlanExpanded "
                f"(supersede old task); digest {old_d[:12]}… → {new_d[:12]}…"
            )


def freeze_plan_rubrics(
    tasks: list[TaskSpec],
    questions: list[QuestionSpec],
    *,
    catalog: Any | None = None,
) -> tuple[list[TaskSpec], list[QuestionSpec]]:
    """Freeze rubric digests on all task outputs; validate final_type rules."""
    frozen_tasks = [freeze_task_rubrics(t) for t in tasks]
    validate_final_types(frozen_tasks, questions, catalog=catalog)
    return frozen_tasks, list(questions)


def validate_plan_final_types_or_err(
    tasks: list[TaskSpec],
    questions: list[QuestionSpec] | list[dict[str, Any]],
    *,
    active_question_ids: set[str] | None = None,
    prev_tasks: list[TaskSpec] | None = None,
    superseded_tasks: set[str] | None = None,
    catalog: Any | None = None,
) -> str | None:
    """Expansion-friendly: return error string or None (never raise to tick)."""
    from pydantic import ValidationError

    try:
        qspecs: list[QuestionSpec] = []
        for q in questions:
            if isinstance(q, QuestionSpec):
                qspecs.append(q)
            else:
                qspecs.append(QuestionSpec.model_validate(q))
        frozen = [freeze_task_rubrics(t) for t in tasks]
        if prev_tasks is not None:
            validate_rubric_immutable(
                new_tasks=frozen,
                prev_tasks=prev_tasks,
                superseded_tasks=superseded_tasks,
            )
        validate_final_types(
            frozen,
            qspecs,
            active_question_ids=active_question_ids,
            catalog=catalog,
        )
        return None
    except (ValueError, ValidationError) as e:
        return str(e) or "invalid_final_type"
