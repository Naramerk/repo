"""Claim stance + tests.intent types (shared; breaks rules↔verdict cycle)."""

from __future__ import annotations

from typing import Literal

from coscientist.core.events.errors import JournalFormatError

STANCE = {"met": "supports", "not_met": "refutes", "unassessed": "inconclusive"}

TestsIntent = Literal["confirm", "refute"]
# Named export for callers that pass intent= explicitly. Not a silent model field default.
DEFAULT_TESTS_INTENT: TestsIntent = "confirm"  # audit-ok: named TestsIntent wire default (not silent invent)


def claim_stance(criterion_status: str) -> Literal["supports", "refutes", "inconclusive"]:
    try:
        return STANCE[criterion_status]  # type: ignore[return-value]
    except KeyError as e:
        raise JournalFormatError(
            f"unknown criterion_status: {criterion_status!r}"
        ) from e
