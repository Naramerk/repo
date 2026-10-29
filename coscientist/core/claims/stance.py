"""Claim stance + tests.intent types (shared; breaks rules↔verdict cycle)."""

from __future__ import annotations

from typing import Literal

ClaimStance = Literal["supports", "refutes", "inconclusive"]
STANCE: dict[str, ClaimStance] = {
    "met": "supports",
    "not_met": "refutes",
    "unassessed": "inconclusive",
}

TestsIntent = Literal["confirm", "refute"]
DEFAULT_TESTS_INTENT: TestsIntent = "confirm"


def claim_stance(criterion_status: str) -> ClaimStance:
    return STANCE[criterion_status]
