"""Claim stance + tests.intent types (shared; breaks rules↔verdict cycle).

``DEFAULT_TESTS_INTENT`` is for **explicit** construction sites only
(dataclass field when the caller omitted intent). Verdict/rules paths must
require intent or refuse — never ``or DEFAULT_TESTS_INTENT``.
"""

from __future__ import annotations

from typing import Literal

STANCE = {"met": "supports", "not_met": "refutes", "unassessed": "inconclusive"}

TestsIntent = Literal["confirm", "refute"]
DEFAULT_TESTS_INTENT: TestsIntent = "confirm"


def claim_stance(criterion_status: str) -> Literal["supports", "refutes", "inconclusive"]:
    return STANCE[criterion_status]  # type: ignore[return-value]
