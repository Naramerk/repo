"""Judge / ReviewCheck apply path (M1 PR13).

``Executor.record_review`` populates ``review_statuses`` keyed by
``check_id`` or ``check_id:item_id``, emits ``ReviewRecorded``, and
re-runs evaluation for an open attempt.
"""

from __future__ import annotations

from typing import Any, Literal

from coscientist.checks.kinds.rubric import JUDGE_FAILURE_KINDS
from coscientist.core.domain import ReqStatus
from coscientist.core.events import EventEnvelope, make_idem

JudgeFailureKind = Literal["timeout", "invalid_json", "refusal"]


def resolve_review_status(
    *,
    status: ReqStatus | str | None,
    failure_kind: str | None,
    prior_retries: int,
) -> tuple[ReqStatus, int, str | None]:
    """Map a review answer to status + updated retry count.

    - ``failure_kind`` in timeout|invalid_json|refusal → UNASSESSED, retry += 1
    - explicit FAILED (not-met) → FAILED (never remapped)
    - MET / WAIVED / … → as given
    """
    if failure_kind is not None and failure_kind in JUDGE_FAILURE_KINDS:
        return ReqStatus.UNASSESSED, prior_retries + 1, failure_kind
    if status is None:
        raise ValueError("record_review requires status or failure_kind")
    st = status if isinstance(status, ReqStatus) else ReqStatus(str(status))
    return st, prior_retries, None


def review_scheduled_events(
    *,
    study_id: str,
    attempt_id: str,
    checks: list[Any],
    now: Any,
) -> list[EventEnvelope]:
    """Emit ReviewScheduled per pending/unassessed judge item (and whole ReviewChecks)."""
    events: list[EventEnvelope] = []
    seen: set[str] = set()
    for c in checks:
        effect = getattr(c, "effect", None) or {}
        item_statuses = effect.get("item_statuses") if isinstance(effect, dict) else None
        if isinstance(item_statuses, dict):
            pending = set(effect.get("pending_items") or [])
            for item_id, st in item_statuses.items():
                if st not in (
                    ReqStatus.PENDING.value,
                    ReqStatus.UNASSESSED.value,
                    "PENDING",
                    "UNASSESSED",
                ) and item_id not in pending:
                    continue
                key = f"{c.check_id}:{item_id}"
                if key in seen:
                    continue
                seen.add(key)
                events.append(
                    EventEnvelope(
                        study_id=study_id,
                        type="ReviewScheduled",
                        entity_key=attempt_id,
                        idem_key=make_idem("ReviewScheduled", attempt_id, key),
                        payload={
                            "check_id": key,
                            "parent_check_id": c.check_id,
                            "item_id": item_id,
                            "attempt_id": attempt_id,
                            "kind": "rubric_judge",
                            "rubric_digest": effect.get("rubric_digest"),
                        },
                        ts=now,
                    )
                )
            continue
        if not getattr(c, "is_code", True) and getattr(c, "status", None) in (
            ReqStatus.UNASSESSED,
            ReqStatus.PENDING,
        ):
            key = str(c.check_id)
            if key in seen:
                continue
            seen.add(key)
            events.append(
                EventEnvelope(
                    study_id=study_id,
                    type="ReviewScheduled",
                    entity_key=attempt_id,
                    idem_key=make_idem("ReviewScheduled", attempt_id, key),
                    payload={
                        "check_id": key,
                        "attempt_id": attempt_id,
                        "kind": "review_check",
                    },
                    ts=now,
                )
            )
    return events
