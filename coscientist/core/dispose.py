"""QuestionDisposed event + validation (M1 PR15 / DESIGN_M1 §2, §6.4).

Explicit dispositions are only ``needs_more_data`` | ``infeasible``. They apply
until the first counting claim. ``needs_more_data`` additionally requires an
``experiment_design`` report artifact (rubric MET).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.experiment_design import (
    MISSING_DESIGN_LINE,
    MISSING_DESIGN_REASON,
    design_artifact_ok,
)
from coscientist.core.reasons import Disposition

ExplicitDisposition = Literal["needs_more_data", "infeasible"]
EXPLICIT_DISPOSITIONS: frozenset[str] = frozenset({"needs_more_data", "infeasible"})

# Ref kinds accepted in QuestionDisposed.refs (≥1 required).
VALID_REF_PREFIXES: tuple[str, ...] = (
    "evidence:",
    "limitation:",
    "hitl:",
    "art_",
    "claim:",
)


class QuestionDisposal(BaseModel):
    """Typed agent/core request to dispose a question without a counting claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str
    disposition: ExplicitDisposition
    reason: str = ""
    refs: list[str] = Field(default_factory=list)
    # Required for needs_more_data (artifact_id of experiment_design report).
    design_artifact_id: str | None = None


class DisposeRejected(ValueError):
    """Disposal refused (missing refs / design / unknown question)."""


def _refs_ok(refs: list[str]) -> bool:
    if not refs:
        return False
    for r in refs:
        s = str(r or "")
        if not s:
            return False
        if s.startswith(VALID_REF_PREFIXES) or s.startswith("art_"):
            continue
        # Bare hitl request ids / evidence node ids are accepted when non-empty.
        if len(s) >= 2:
            continue
        return False
    return True


def validate_disposal(
    disposal: QuestionDisposal,
    *,
    known_question_ids: set[str],
    artifact_lookup: Any | None = None,
    tasks: Any | None = None,
    results: Any | None = None,
    covering_task_keys: set[str] | None = None,
) -> None:
    """Raise ``DisposeRejected`` when the disposal is invalid (fail-closed)."""
    if disposal.disposition not in EXPLICIT_DISPOSITIONS:
        raise DisposeRejected(
            f"disposition must be needs_more_data|infeasible, got {disposal.disposition!r}"
        )
    if disposal.question_id not in known_question_ids:
        raise DisposeRejected(f"unknown question id {disposal.question_id!r}")
    if not _refs_ok(list(disposal.refs or [])):
        raise DisposeRejected("QuestionDisposed requires ≥1 ref (evidence|limitation|HitlDecided|artifact)")
    if disposal.disposition == Disposition.NEEDS_MORE_DATA.value:
        if not design_artifact_ok(
            artifact_id=disposal.design_artifact_id,
            artifact_lookup=artifact_lookup,
            tasks=tasks,
            results=results,
            task_keys=covering_task_keys,
        ):
            raise DisposeRejected(f"{MISSING_DESIGN_REASON}: {MISSING_DESIGN_LINE}")


def question_disposed_event(
    *,
    study_id: str,
    disposal: QuestionDisposal,
    now: datetime,
) -> EventEnvelope:
    """Build ``QuestionDisposed`` v1 (idempotent per question_id)."""
    payload = {
        "question_id": disposal.question_id,
        "disposition": disposal.disposition,
        "reason": disposal.reason or "",
        "refs": list(disposal.refs or []),
        "design_artifact_id": disposal.design_artifact_id,
    }
    return EventEnvelope(
        study_id=study_id,
        type="QuestionDisposed",
        entity_key=disposal.question_id,
        idem_key=make_idem("QuestionDisposed", study_id, disposal.question_id),
        payload=payload,
        ts=now,
        schema_version=1,
    )


def fold_question_disposed(
    store: dict[str, dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    """Latest disposal per question_id (journal fold)."""
    qid = str(payload.get("question_id") or "")
    if not qid:
        return
    store[qid] = {
        "question_id": qid,
        "disposition": str(payload.get("disposition") or ""),
        "reason": str(payload.get("reason") or ""),
        "refs": list(payload.get("refs") or []),
        "design_artifact_id": payload.get("design_artifact_id"),
    }


async def dispose_question(
    runtime: Any,
    disposal: QuestionDisposal | dict[str, Any],
) -> str | None:
    """Validate + append QuestionDisposed. Returns question_id or None if rejected.

    Rejection is recorded as a report line / limitation — study still closes.
    Never raises into callers (totality); returns None on reject.
    """
    from coscientist.core.question_tasks import known_questions, question_tasks

    if isinstance(disposal, dict):
        try:
            disposal = QuestionDisposal.model_validate(disposal)
        except Exception:  # noqa: BLE001
            _note_reject(runtime, "invalid_disposal_payload")
            return None
    study = getattr(runtime, "study", None)
    if study is None:
        return None
    sid = study.study_id
    qmap = known_questions(getattr(runtime, "plans", {}) or {})
    cover = question_tasks(getattr(runtime, "plans", {}) or {})
    arts = getattr(runtime, "artifacts", None)
    lookup = getattr(arts, "get_meta", None) if arts is not None else None
    try:
        validate_disposal(
            disposal,
            known_question_ids=set(qmap),
            artifact_lookup=lookup if callable(lookup) else None,
            tasks=getattr(runtime, "tasks", None),
            results=getattr(runtime, "results", None),
            covering_task_keys=set(cover.get(disposal.question_id, set())),
        )
    except DisposeRejected as e:
        _note_reject(runtime, str(e))
        return None
    if getattr(runtime, "_finalized", False):
        return None
    ev = question_disposed_event(
        study_id=sid,
        disposal=disposal,
        now=runtime.clock.now(),
    )
    append = getattr(runtime, "_append", None)
    if append is None:
        return None
    await append([ev])
    return disposal.question_id


def _note_reject(runtime: Any, detail: str) -> None:
    notes = getattr(runtime, "_disposal_rejects", None)
    if not isinstance(notes, list):
        runtime._disposal_rejects = []
        notes = runtime._disposal_rejects
    notes.append({"reason": MISSING_DESIGN_REASON, "detail": str(detail)[:200]})
