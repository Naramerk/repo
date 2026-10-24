"""Shared frozen/strict base for agent typed outs (DESIGN §7.1).

One base + non-blank pattern for ResearchSpec / CriticVerdict / ReviewVerdict —
no per-role copies of SpecModel / CriticModel / ReviewerModel.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# At least one non-whitespace character (rejects "" and "   "; spaces elsewhere OK).
NONBLANK = r".*\S.*"


class AgentModel(BaseModel):
    """Frozen, strict, no extras — role JSON must match the schema exactly."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
