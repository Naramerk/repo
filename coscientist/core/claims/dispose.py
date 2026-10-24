"""QuestionDisposed event + validation (M1 PR15 / DESIGN_M1 §2, §6.4).

Explicit dispositions are only ``needs_more_data`` | ``infeasible``. They apply
until the first counting claim. ``needs_more_data`` additionally requires an
``experiment_design`` report artifact (rubric MET).

Idempotency: first-wins per (study, question) — journal ``idem_key`` and fold
agree (later disposals are no-ops / Idempotent).

Rejected disposals are journaled as ``QuestionDisposeRejected`` (never memory-only)
and surface in finalize ``contributing_reasons``.
"""

from __future__ import annotations

from coscientist.core.events.digest import payload_digest

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coscientist.core.claims.experiment_design import (
    MISSING_DESIGN_LINE,
    MISSING_DESIGN_REASON,
    design_artifact_ok,
)
from coscientist.core.claims.reasons import Disposition
from coscientist.core.events.digest import sha256_hex, payload_digest
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.schema_versions import (
    QUESTION_DISPOSE_REJECTED_SCHEMA_VERSION,
    QUESTION_DISPOSED_SCHEMA_VERSION,
)

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

DISPOSE_REJECTED_LINE_PREFIX = "не выполнено: disposal rejected"
# Live ``_note_reject`` and journaled payload share one truncate length so
# restart/replay digests match (fold re-applies the same detail string).
DISPOSE_REJECT_DETAIL_MAX = 500

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
    """Require ≥1 ref with a known prefix (evidence|limitation|hitl|art_|claim:)."""
    if not refs:
        return False
    for r in refs:
        s = str(r or "")
        if not s:
            return False
        if any(s.startswith(p) for p in VALID_REF_PREFIXES):
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
    journal_kinds: dict[str, str] | None = None,
    pending_design_ids: set[str] | None = None,
) -> None:
    """Raise ``DisposeRejected`` when the disposal is invalid (fail-closed)."""
    del artifact_lookup  # meta.kind alone is never trusted (journal / covering task only)
    if disposal.disposition not in EXPLICIT_DISPOSITIONS:
        raise DisposeRejected(
            f"disposition must be needs_more_data|infeasible, got {disposal.disposition!r}"
        )
    if disposal.question_id not in known_question_ids:
        raise DisposeRejected(f"unknown question id {disposal.question_id!r}")
    if not _refs_ok(list(disposal.refs or [])):
        raise DisposeRejected(
            "QuestionDisposed requires ≥1 ref (evidence:|limitation:|hitl:|art_|claim:)"
        )
    if disposal.disposition == Disposition.NEEDS_MORE_DATA.value:
        if not design_artifact_ok(
            artifact_id=disposal.design_artifact_id,
            journal_kinds=journal_kinds,
            pending_design_ids=pending_design_ids,
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
    """Build ``QuestionDisposed`` v1 (idempotent per question_id — first-wins)."""
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
        schema_version=QUESTION_DISPOSED_SCHEMA_VERSION,
                    epoch=0,
                    payload_digest=payload_digest(payload),
                )

def dispose_reject_report_line(detail: str) -> str:
    """Russian contributing_reasons line for a rejected disposal."""
    d = str(detail or "")
    if MISSING_DESIGN_REASON in d or MISSING_DESIGN_LINE in d:
        return MISSING_DESIGN_LINE
    short = d[:120] if d else "unknown"
    return f"{DISPOSE_REJECTED_LINE_PREFIX}: {short}"

def question_dispose_rejected_event(
    *,
    study_id: str,
    detail: str,
    now: datetime,
    question_id: str | None = None,
    disposition: str | None = None,
) -> EventEnvelope:
    """Journal a rejected disposal (survives restart; feeds contributing_reasons)."""
    qid = str(question_id or "")
    detail_s = str(detail or "")[:DISPOSE_REJECT_DETAIL_MAX]
    digest = sha256_hex(detail_s)[:16]
    entity = qid or study_id
    payload = {
        "question_id": qid or None,
        "disposition": disposition,
        "detail": detail_s,
        "reason": MISSING_DESIGN_REASON if MISSING_DESIGN_REASON in detail_s else "dispose_rejected",
        "report_line": dispose_reject_report_line(detail_s),
    }
    return EventEnvelope(
        study_id=study_id,
        type="QuestionDisposeRejected",
        entity_key=entity,
        idem_key=make_idem("QuestionDisposeRejected", study_id, entity, digest),
        payload=payload,
        ts=now,
        schema_version=QUESTION_DISPOSE_REJECTED_SCHEMA_VERSION,
                    epoch=0,
                    payload_digest=payload_digest(payload),
                )

def fold_question_disposed(
    store: dict[str, dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    """First-wins per question_id (matches journal idempotency)."""
    qid = str(payload.get("question_id") or "")
    if not qid:
        return
    if qid in store:
        return
    store[qid] = {
        "question_id": qid,
        "disposition": str(payload.get("disposition") or ""),
        "reason": str(payload.get("reason") or ""),
        "refs": list(payload.get("refs") or []),
        "design_artifact_id": payload.get("design_artifact_id"),
    }

def fold_question_dispose_rejected(
    store: list[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    """Append rejection note (idempotent by detail digest in payload)."""
    row = {
        "question_id": payload.get("question_id"),
        "disposition": payload.get("disposition"),
        "detail": str(payload.get("detail") or ""),
        "reason": str(payload.get("reason") or ""),
        "report_line": str(payload.get("report_line") or dispose_reject_report_line(str(payload.get("detail") or ""))),
    }
    dig = sha256_hex(str(row["detail"]))[:16]
    for existing in store:
        if sha256_hex(str(existing.get("detail") or ""))[:16] == dig and existing.get(
            "question_id"
        ) == row["question_id"]:
            return
    store.append(row)

async def dispose_question(
    runtime: Any,
    disposal: QuestionDisposal | dict[str, Any],
) -> str | None:
    """Validate + append QuestionDisposed. Returns question_id or None if rejected.

    Rejection is journaled as ``QuestionDisposeRejected`` — study still closes.
    ``LeaseLost`` always propagates. Post-``StudyFinalized`` writes return None
    (nothing may be appended after finalize).
    """
    from coscientist.core.events.errors import LeaseLost, StudyClosedError
    from coscientist.core.plan.question_tasks import known_questions, question_tasks

    study = getattr(runtime, "study", None)
    if study is None:
        return None
    sid = study.study_id
    now = runtime.clock.now()
    qid_hint: str | None = None
    disp_hint: str | None = None

    if isinstance(disposal, dict):
        qid_hint = str(disposal.get("question_id") or "") or None
        disp_hint = str(disposal.get("disposition") or "") or None
        try:
            disposal = QuestionDisposal.model_validate(disposal)
        except ValidationError:
            await _journal_reject(
                runtime,
                study_id=sid,
                detail="invalid_disposal_payload",
                now=now,
                question_id=qid_hint,
                disposition=disp_hint,
            )
            return None
    qid_hint = disposal.question_id
    disp_hint = disposal.disposition
    qmap = known_questions(getattr(runtime, "plans", {}) or {})
    cover = question_tasks(getattr(runtime, "plans", {}) or {})
    kinds = getattr(runtime, "artifact_kinds", None)
    try:
        validate_disposal(
            disposal,
            known_question_ids=set(qmap),
            journal_kinds=kinds if isinstance(kinds, dict) else None,
            tasks=getattr(runtime, "tasks", None),
            results=getattr(runtime, "results", None),
            covering_task_keys=set(cover.get(disposal.question_id, set())),
        )
    except DisposeRejected as e:
        await _journal_reject(
            runtime,
            study_id=sid,
            detail=str(e),
            now=now,
            question_id=qid_hint,
            disposition=disp_hint,
        )
        return None
    if getattr(runtime, "_finalized", False):
        return None
    ev = question_disposed_event(
        study_id=sid,
        disposal=disposal,
        now=now,
    )
    append = getattr(runtime, "_append", None)
    if append is None:
        return None
    try:
        await append([ev])
    except LeaseLost:
        raise
    except StudyClosedError:
        return None
    return disposal.question_id

async def _journal_reject(
    runtime: Any,
    *,
    study_id: str,
    detail: str,
    now: datetime,
    question_id: str | None = None,
    disposition: str | None = None,
) -> None:
    """Append QuestionDisposeRejected; LeaseLost propagates; StudyClosed → memory note only."""
    from coscientist.core.events.errors import LeaseLost, StudyClosedError

    ev = question_dispose_rejected_event(
        study_id=study_id,
        detail=detail,
        now=now,
        question_id=question_id,
        disposition=disposition,
    )
    if getattr(runtime, "_finalized", False):
        return
    append = getattr(runtime, "_append", None)
    if append is None:
        return
    try:
        await append([ev])
    except LeaseLost:
        raise
    except StudyClosedError:
        return
    # Live mirror only after durable append; fold re-applies on replay.
    _note_reject(runtime, detail, report_line=ev.payload.get("report_line"))

def _note_reject(
    runtime: Any,
    detail: str,
    *,
    report_line: str | None = None,
) -> None:
    """Mirror a reject into live memory (idempotent by detail digest)."""
    notes = getattr(runtime, "_disposal_rejects", None)
    if not isinstance(notes, list):
        runtime._disposal_rejects = []
        notes = runtime._disposal_rejects
    detail_s = str(detail)[:DISPOSE_REJECT_DETAIL_MAX]
    dig = sha256_hex(detail_s)[:16]
    for existing in notes:
        if sha256_hex(str(existing.get("detail") or ""))[:16] == dig:
            return
    notes.append(
        {
            "reason": MISSING_DESIGN_REASON,
            "detail": detail_s,
            "report_line": report_line or dispose_reject_report_line(detail_s),
        }
    )
