"""Shared frozen/strict base for planner and executor-route schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AgentModel(BaseModel):
    """Frozen strict role models — no extras, no silent coercion."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
