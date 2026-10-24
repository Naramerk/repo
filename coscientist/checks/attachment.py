"""Capability #7 helpers: CodeCheck + ReviewCheck on a result; UNASSESSED ≠ FAILED.

Pure functions over check-result rows. Executor wiring is a later PR.
No status coercion, no guessed defaults — missing attachment → CatalogError.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from coscientist.core.events.errors import CatalogError
from coscientist.core.execution.domain import ReqStatus


class CheckLike(Protocol):
    """Narrow surface for attachment checks (avoids importing full CheckResult)."""

    @property
    def check_id(self) -> str: ...

    @property
    def status(self) -> ReqStatus: ...

    @property
    def is_code(self) -> bool: ...

    @property
    def required(self) -> bool: ...


def code_checks(checks: Sequence[CheckLike]) -> list[CheckLike]:
    """Return rows whose catalog check is a CodeCheck (``is_code``)."""
    return [c for c in checks if c.is_code]


def review_checks(checks: Sequence[CheckLike]) -> list[CheckLike]:
    """Return rows whose catalog check is a ReviewCheck (not ``is_code``)."""
    return [c for c in checks if not c.is_code]


def require_code_and_review(checks: Sequence[CheckLike]) -> tuple[list[CheckLike], list[CheckLike]]:
    """Require ≥1 CodeCheck and ≥1 ReviewCheck on a result record.

    Returns ``(code, review)``. Raises ``CatalogError`` when either side is empty
    (typed refuse — CAPABILITIES #7: both kinds attached on every result).
    """
    code = code_checks(checks)
    review = review_checks(checks)
    if not code:
        raise CatalogError("result missing CodeCheck attachment")
    if not review:
        raise CatalogError("result missing ReviewCheck attachment")
    return code, review


def unassessed_required_review_ids(checks: Sequence[CheckLike]) -> list[str]:
    """Ids of required ReviewChecks still without a verdict (R23).

    Only ``UNASSESSED`` and ``PENDING`` count. ``FAILED`` is a verdict and must
    not appear here — UNASSESSED is distinct from FAILED.
    """
    out: list[str] = []
    for c in review_checks(checks):
        if not c.required:
            continue
        if c.status in (ReqStatus.UNASSESSED, ReqStatus.PENDING):
            out.append(c.check_id)
    return out


def refuse_unassessed_failed_coercion(from_status: ReqStatus, to_status: ReqStatus) -> None:
    """Refuse remapping UNASSESSED ↔ FAILED (R23).

    Any other pair is allowed (caller decides). Same-status is a no-op.
    """
    if from_status == to_status:
        return
    pair = {from_status, to_status}
    if pair == {ReqStatus.UNASSESSED, ReqStatus.FAILED}:
        raise CatalogError(
            f"UNASSESSED must stay distinct from FAILED; refused {from_status.value!r}→{to_status.value!r}"
        )
