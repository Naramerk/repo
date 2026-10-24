"""Capability #7 helpers: CodeCheck + ReviewCheck on a result; UNASSESSED ≠ FAILED.

Pure functions over check-result rows. Executor wiring is Q3.
``CheckLike`` / agent view use ``is_code`` (same owner as ``CheckResult``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from coscientist.core.events.errors import CheckAttachmentError
from coscientist.core.execution.domain import ReqStatus

UNASSESSED_STATUSES: frozenset[ReqStatus] = frozenset({ReqStatus.UNASSESSED, ReqStatus.PENDING})

# DESIGN §5.4 partners for UNASSESSED (R23: shown separately from FAILED/STALE/
# MIGRATION_ERROR). PENDING = in_progress; MET|WAIVED|NOT_APPLICABLE = done.
UNASSESSED_PARTNERS: frozenset[ReqStatus] = frozenset(
    {
        ReqStatus.PENDING,
        ReqStatus.MET,
        ReqStatus.WAIVED,
        ReqStatus.NOT_APPLICABLE,
    }
)
_UNASSESSED_TRANSITIONS: frozenset[tuple[ReqStatus, ReqStatus]] = frozenset(
    {(ReqStatus.UNASSESSED, s) for s in UNASSESSED_PARTNERS} | {(s, ReqStatus.UNASSESSED) for s in UNASSESSED_PARTNERS}
)


class CheckLike(Protocol):
    """Narrow surface — mirrors ``CheckResult`` (``is_code`` owner)."""

    @property
    def check_id(self) -> str: ...

    @property
    def status(self) -> ReqStatus: ...

    @property
    def is_code(self) -> bool: ...

    @property
    def required(self) -> bool: ...


def code_checks(checks: Sequence[CheckLike]) -> list[CheckLike]:
    """Rows with ``is_code`` (CodeCheck attachment)."""
    return [c for c in checks if c.is_code]


def review_checks(checks: Sequence[CheckLike]) -> list[CheckLike]:
    """Rows with ReviewCheck attachment (``not is_code`` — same owner as CheckResult)."""
    return [c for c in checks if not c.is_code]


def require_code_and_review(checks: Sequence[CheckLike]) -> tuple[list[CheckLike], list[CheckLike]]:
    """Require ≥1 CodeCheck and ≥1 ReviewCheck; miss → ``CheckAttachmentError``."""
    code = code_checks(checks)
    review = review_checks(checks)
    if not code:
        raise CheckAttachmentError("result missing CodeCheck attachment")
    if not review:
        raise CheckAttachmentError("result missing ReviewCheck attachment")
    return code, review


def unassessed_required_review_ids(checks: Sequence[CheckLike]) -> list[str]:
    """Required ReviewCheck ids still without verdict (R23).

    Only ``UNASSESSED``|``PENDING``. FAILED/STALE/MIGRATION_ERROR excluded
    (DESIGN §5.4 — UNASSESSED shown separately; those statuses are distinct).
    """
    out: list[str] = []
    for c in review_checks(checks):
        if not c.required:
            continue
        if c.status in UNASSESSED_STATUSES:
            out.append(c.check_id)
    return out


def refuse_illegal_unassessed_transition(from_status: ReqStatus, to_status: ReqStatus) -> None:
    """Refuse illegal UNASSESSED transitions (allowlist ``_UNASSESSED_TRANSITIONS``)."""
    if from_status == to_status:
        return
    # PENDING→FAILED/STALE etc. never touch UNASSESSED — out of scope here.
    if from_status is not ReqStatus.UNASSESSED and to_status is not ReqStatus.UNASSESSED:
        return
    if (from_status, to_status) in _UNASSESSED_TRANSITIONS:
        return
    raise CheckAttachmentError(f"UNASSESSED must stay distinct from {from_status.value!r}→{to_status.value!r}")
