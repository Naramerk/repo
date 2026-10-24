"""Job* journal payloads (DESIGN §6.4). Validated at emit; fold is graph G19.

``deadline`` is an aware ISO-8601 string on the wire (JSON-only journal rule).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

JobKind = Literal["coder", "alembic"]
JobStatus = Literal[
    "queued", "running", "succeeded", "failed", "timed_out", "cancelled"
]

_LIVE: frozenset[str] = frozenset({"queued", "running"})
_TERMINAL: frozenset[str] = frozenset(
    {"succeeded", "failed", "timed_out", "cancelled"}
)


def job_is_live(status: JobStatus) -> bool:
    return status in _LIVE


def job_is_terminal(status: JobStatus) -> bool:
    return status in _TERMINAL


def deadline_to_wire(deadline: datetime) -> str:
    if deadline.tzinfo is None:
        raise ValueError("deadline must be timezone-aware")
    return deadline.isoformat().replace("+00:00", "Z")


class _JobPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    job_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)

    @field_validator("job_id", "attempt_id")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if v.strip() != v or not v.strip():
            raise ValueError("must be non-empty without surrounding whitespace")
        return v


class JobSubmittedPayload(_JobPayload):
    kind: JobKind
    deadline: str = Field(min_length=1)
    spec_digest: str = Field(min_length=1)

    @field_validator("deadline", "spec_digest")
    @classmethod
    def _non_blank_fields(cls, v: str) -> str:
        if v.strip() != v or not v.strip():
            raise ValueError("must be non-empty without surrounding whitespace")
        return v


class JobStatusChangedPayload(_JobPayload):
    status: JobStatus


class JobTimedOutPayload(_JobPayload):
    deadline: str = Field(min_length=1)

    @field_validator("deadline")
    @classmethod
    def _deadline(cls, v: str) -> str:
        if v.strip() != v or not v.strip():
            raise ValueError("must be non-empty without surrounding whitespace")
        return v


class JobCancelledPayload(_JobPayload):
    pass


class JobCancelFailedPayload(_JobPayload):
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        if v.strip() != v or not v.strip():
            raise ValueError("reason must be non-empty without surrounding whitespace")
        return v
