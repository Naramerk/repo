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

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coscientist.core.claims.experiment_design import (
    MISSING_DESIGN_LINE,
    MISSING_DESIGN_REASON,
    design_artifact_ok,
)
from coscientist.core.claims.reasons import Disposition
from coscientist.core.events.digest import sha256_hex
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.schema_versions import (
    QUESTION_DISPOSE_REJECTED_SCHEMA_VERSION,
    QUESTION_DISPOSED_SCHEMA_VERSION,
)

ExplicitDisposition = Literal["needs_more_data", "infeasible"]
EXPLICIT_DISPOSITIONS: frozenset[str] = frozenset({"needs_more_data", "infeasible"})

DisposeRefKind = Literal["evidence", "limitation", "hitl", "artifact", "claim"]
VALID_REF_KINDS: frozenset[str] = frozenset(
    {"evidence", "limitation", "hitl", "artifact", "claim"}
)


class DisposeRef(BaseModel):
    """Typed disposal ref — kind is explicit (never inferred from string prefix)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: DisposeRefKind
    id: str


# Legacy wire prefixes (tests/docs); validation uses DisposeRef.kind only.
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
    reason: str  # required; empty str only when caller passes it explicitly
    refs: list[DisposeRef | dict[str, Any]] = Field(default_factory=list)
    # Required for needs_more_data (artifact_id of experiment_design report).
    design_artifact_id: str | None = None


class DisposeRejected(ValueError):
    """Disposal refused (missing refs / design / unknown question)."""


def _coerce_dispose_ref(r: object) -> DisposeRef:
    """Accept DisposeRef or ``{kind, id}`` dict; refuse prefix-only strings."""
    if isinstance(r, DisposeRef):
        if not r.id:
            raise DisposeRejected("dispose ref id must be non-empty")
        return r
    if isinstance(r, dict):
        try:
            ref = DisposeRef.model_validate(r)
        except ValidationError as e:
            raise DisposeRejected(f"dispose ref invalid: {e}") from e
        if not ref.id:
            raise DisposeRejected("dispose ref id must be non-empty")
        return ref
    raise DisposeRejected(
        f"dispose ref must be {{kind, id}} (typed); string prefixes removed, got {type(r).__name__}"
    )


def _refs_ok(refs: list[Any]) -> bool:
    """Require ≥1 typed DisposeRef with a known kind."""
    if not refs:
        return False
    for r in refs:
        try:
            _coerce_dispose_ref(r)
        except DisposeRejected:
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
    if not _refs_ok(list(disposal.refs)):
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
) -> UnstampedEvent:
    """Build ``QuestionDisposed`` v1 (idempotent per question_id — first-wins)."""
    payload = {
        "question_id": disposal.question_id,
        "disposition": disposal.disposition,
        "reason": disposal.reason,
        "refs": [
            (
                r.model_dump(mode="json")
                if isinstance(r, DisposeRef)
                else dict(r)
            )  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
            for r in disposal.refs
        ],
        "design_artifact_id": disposal.design_artifact_id,
    }
    return UnstampedEvent(
        study_id=study_id,
        type="QuestionDisposed",
        entity_key=disposal.question_id,
        idem_key=make_idem("QuestionDisposed", study_id, disposal.question_id),
        payload=payload,
        ts=now,
        schema_version=QUESTION_DISPOSED_SCHEMA_VERSION,
    )


def dispose_reject_report_line(detail: str) -> str:
    """Russian contributing_reasons line for a rejected disposal."""
    if not isinstance(detail, str):
        raise TypeError(f"dispose detail must be str, got {type(detail).__name__}")
    if MISSING_DESIGN_REASON in detail or MISSING_DESIGN_LINE in detail:
        return MISSING_DESIGN_LINE
    if detail:
        short = detail[:120]
    else:
        raise ValueError("dispose_reject_report_line requires non-empty detail")
    return f"{DISPOSE_REJECTED_LINE_PREFIX}: {short}"


def question_dispose_rejected_event(
    *,
    study_id: str,
    detail: str,
    now: datetime,
    question_id: str | None = None,
    disposition: str | None = None,
) -> UnstampedEvent:
    """Journal a rejected disposal (survives restart; feeds contributing_reasons)."""
    if question_id is None:
        qid = ""
    else:
        if not isinstance(question_id, str):
            raise TypeError(f"question_id must be str, got {type(question_id).__name__}")
        qid = question_id
    if not isinstance(detail, str):
        raise TypeError(f"detail must be str, got {type(detail).__name__}")
    detail_s = detail[:DISPOSE_REJECT_DETAIL_MAX]
    digest = sha256_hex(detail_s)[:16]
    entity = qid if qid else study_id  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
    if MISSING_DESIGN_REASON in detail_s:
        reason = MISSING_DESIGN_REASON
    else:
        reason = "dispose_rejected"
    payload = {
        "question_id": qid or None,
        "disposition": disposition,
        "detail": detail_s,
        "reason": reason,
        "report_line": dispose_reject_report_line(detail_s),
    }
    return UnstampedEvent(
        study_id=study_id,
        type="QuestionDisposeRejected",
        entity_key=entity,
        idem_key=make_idem("QuestionDisposeRejected", study_id, entity, digest),
        payload=payload,
        ts=now,
        schema_version=QUESTION_DISPOSE_REJECTED_SCHEMA_VERSION,
    )


def _require_payload_str(payload: dict[str, Any], key: str, *, what: str) -> str:
    if key not in payload:
        raise JournalFormatError(f"{what}: missing {key!r}")
    raw = payload[key]
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise JournalFormatError(
            f"{what}: {key} must be str|None, got {type(raw).__name__}"
        )
    return raw


def fold_question_disposed(
    store: dict[str, dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    """First-wins per question_id (matches journal idempotency)."""
    qid = _require_payload_str(payload, "question_id", what="QuestionDisposed")
    if not qid:
        return
    if qid in store:
        return
    if "refs" not in payload:
        raise JournalFormatError("QuestionDisposed missing refs")
    refs = payload["refs"]
    if refs is None:
        refs_list: list[Any] = []  # audit-ok[field_default]: structural empty = null refs ≡ absent
    elif isinstance(refs, list):
        refs_list = list(refs)
    else:
        raise JournalFormatError(
            f"QuestionDisposed refs must be list|None, got {type(refs).__name__}"
        )
    store[qid] = {
        "question_id": qid,
        "disposition": _require_payload_str(payload, "disposition", what="QuestionDisposed"),
        "reason": _require_payload_str(payload, "reason", what="QuestionDisposed"),
        "refs": refs_list,
        "design_artifact_id": payload.get("design_artifact_id"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    }


def fold_question_dispose_rejected(
    store: list[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    """Append rejection note (idempotent by detail digest in payload)."""
    detail = _require_payload_str(payload, "detail", what="QuestionDisposeRejected")
    if "report_line" in payload and payload["report_line"] is not None:
        report_line = payload["report_line"]
        if not isinstance(report_line, str):
            raise JournalFormatError(
                f"QuestionDisposeRejected report_line must be str, got {type(report_line).__name__}"
            )
    else:
        report_line = dispose_reject_report_line(detail)
    row = {
        "question_id": payload.get("question_id"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        "disposition": payload.get("disposition"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        "detail": detail,
        "reason": _require_payload_str(payload, "reason", what="QuestionDisposeRejected"),
        "report_line": report_line,
    }
    dig = sha256_hex(detail)[:16]
    for existing in store:
        if "detail" not in existing:
            raise JournalFormatError("disposal reject note missing detail")
        ex_detail = existing["detail"]
        if not isinstance(ex_detail, str):
            raise JournalFormatError(
                f"disposal reject detail must be str, got {type(ex_detail).__name__}"
            )
        if sha256_hex(ex_detail)[:16] == dig and existing.get("question_id") == row[  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            "question_id"
        ]:
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

    try:
        study = runtime.study
    except AttributeError as e:
        raise JournalFormatError("dispose_question: runtime missing study") from e
    if study is None:
        return None
    sid = study.study_id
    now = runtime.clock.now()
    qid_hint: str | None = None
    disp_hint: str | None = None

    if isinstance(disposal, dict):
        raw_qid = disposal.get("question_id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if raw_qid is None or raw_qid == "":
            qid_hint = None
        elif isinstance(raw_qid, str):
            qid_hint = raw_qid
        else:
            raise TypeError(f"question_id must be str, got {type(raw_qid).__name__}")
        raw_disp = disposal.get("disposition")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if raw_disp is None or raw_disp == "":
            disp_hint = None
        elif isinstance(raw_disp, str):
            disp_hint = raw_disp
        else:
            raise TypeError(f"disposition must be str, got {type(raw_disp).__name__}")
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
    try:
        plans = runtime.plans
    except AttributeError as e:
        raise JournalFormatError("dispose_question: runtime missing plans") from e
    if plans is None:
        raise JournalFormatError("dispose_question: runtime.plans is None")
    qmap = known_questions(plans)
    cover = question_tasks(plans)
    try:
        kinds = runtime.artifact_kinds
    except AttributeError:
        kinds = None
    try:
        tasks = runtime.tasks
    except AttributeError:
        tasks = None
    try:
        results = runtime.results
    except AttributeError:
        results = None
    try:
        validate_disposal(
            disposal,
            known_question_ids=set(qmap),
            journal_kinds=kinds if isinstance(kinds, dict) else None,  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
            tasks=tasks,
            results=results,
            covering_task_keys=set(cover.get(disposal.question_id, set())),  # audit-ok[dict_get_default]: explicit default only for structural absent key
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
    try:
        finalized = runtime._finalized
    except AttributeError as e:
        raise JournalFormatError("dispose_question: runtime missing _finalized") from e
    if finalized:
        return None
    ev = question_disposed_event(
        study_id=sid,
        disposal=disposal,
        now=now,
    )
    try:
        append = runtime._append
    except AttributeError:
        return None  # audit-ok[except_silent_return]: typed refuse/None on expected parse/IO class
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
    try:
        finalized = runtime._finalized
    except AttributeError as e:
        raise JournalFormatError("_journal_reject: runtime missing _finalized") from e
    if finalized:
        return
    try:
        append = runtime._append
    except AttributeError:
        return  # audit-ok[except_silent_return]: typed refuse/None on expected parse/IO class
    try:
        await append([ev])
    except LeaseLost:
        raise
    except StudyClosedError:
        return
    # Live mirror only after durable append; fold re-applies on replay.
    if "report_line" not in ev.payload:
        raise JournalFormatError("QuestionDisposeRejected missing report_line")
    _note_reject(runtime, detail, report_line=ev.payload["report_line"])


def _note_reject(
    runtime: Any,
    detail: str,
    *,
    report_line: str | None = None,
) -> None:
    """Mirror a reject into live memory (idempotent by detail digest)."""
    try:
        notes = runtime._disposal_rejects
    except AttributeError:
        runtime._disposal_rejects = []
        notes = runtime._disposal_rejects
    if not isinstance(notes, list):
        raise JournalFormatError(
            f"_disposal_rejects must be list, got {type(notes).__name__}"
        )
    if not isinstance(detail, str):
        raise TypeError(f"detail must be str, got {type(detail).__name__}")
    detail_s = detail[:DISPOSE_REJECT_DETAIL_MAX]
    dig = sha256_hex(detail_s)[:16]
    for existing in notes:
        if "detail" not in existing:
            raise JournalFormatError("disposal reject note missing detail")
        ex_detail = existing["detail"]
        if not isinstance(ex_detail, str):
            raise JournalFormatError(
                f"disposal reject detail must be str, got {type(ex_detail).__name__}"
            )
        if sha256_hex(ex_detail)[:16] == dig:
            return
    if report_line is None:
        line = dispose_reject_report_line(detail_s)
    else:
        line = report_line
    notes.append(
        {
            "reason": MISSING_DESIGN_REASON,
            "detail": detail_s,
            "report_line": line,
        }
    )
