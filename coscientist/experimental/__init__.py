"""Beyond-M0 surfaces parked until their milestone.

Not part of the M0 state-core API (``coscientist.core``).

| Module | Content | Target milestone |
|--------|---------|------------------|
| ``costly`` | ``DelegatedBudget``, ``costly_gate``, ``settle`` | M2 (HITL §7.3) |
| ``hitl`` / ``hitl_costly`` | HITL gates, fork stub, invariants | M2 |
| ``report`` | HITL-spend report stub | M5 |
| ``runtime`` | ``StudyRuntime`` = core + HITL/costly mixins | M2 |

Requirement catalog lives in ``coscientist.checks`` (M1), not core.
"""

from coscientist.experimental.costly import (
    CostlyEffect,
    DelegatedBudget,
    costly_gate,
    settle,
)
from coscientist.experimental.report import render, render_hitl_spend
from coscientist.experimental.runtime import StudyRuntime

__all__ = [
    "CostlyEffect",
    "DelegatedBudget",
    "StudyRuntime",
    "costly_gate",
    "render",
    "render_hitl_spend",
    "settle",
]
