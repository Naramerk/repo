"""Job* journal payloads (DESIGN §6.4). Validated at emit; fold is graph G19.

``deadline`` is an aware ISO-8601 UTC string on the wire (JSON-only journal rule).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

JobKind = Literal["coder", "alembic"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "timed_out", "cancelled"]
JobEventType = Literal[
    "JobSubmitted",
    "JobStatusChanged",
    "JobTimedOut",
    "JobCancelled",
    "JobCancelFailed",
]

_LIVE: frozenset[JobStatus] = frozenset({"queued", "running"})
_TERMINAL: frozenset[JobStatus] = frozenset({"succeeded", "failed", "timed_out", "cancelled"})
# cancel() refuses these; timed_out still accepts cancel → JobCancelled (R4).
_CANCEL_REFUSED: frozenset[JobStatus] = frozenset({"succeeded", "failed", "cancelled"})
# These statuses use dedicated JobTimedOut / JobCancelled events, not StatusChanged.
_STATUS_CHANGED_SKIP: frozenset[JobStatus] = frozenset({"timed_out", "cancelled"})

JOB_ID_DIGEST_HEX_LEN = 16


def job_is_live(status: JobStatus) -> bool:
    return status in _LIVE


def job_is_terminal(status: JobStatus) -> bool:
    return status in _TERMINAL


def job_cancel_refused(status: JobStatus) -> bool:
    """True when cancel must emit JobCancelFailed(reason=status=...)."""
    return status in _CANCEL_REFUSED


def job_emits_status_changed(status: JobStatus) -> bool:
    """False for statuses that have their own journal event type."""
    return status not in _STATUS_CHANGED_SKIP


def require_nonblank(value: str, *, field: str) -> str:
    if value.strip() != value or not value.strip():
        raise ValueError(f"{field} must be non-empty without surrounding whitespace")
    return value


def require_aware_datetime(value: datetime, *, field: str = "deadline") -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def deadline_to_wire(deadline: datetime) -> str:
    utc = require_aware_datetime(deadline, field="deadline").astimezone(UTC)
    return utc.isoformat().replace("+00:00", "Z")


class _JobPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    job_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)

    @field_validator("job_id", "attempt_id")
    @classmethod
    def _non_blank(cls, v: str, info: ValidationInfo) -> str:
        return require_nonblank(v, field=str(info.field_name))


class JobSubmittedPayload(_JobPayload):
    kind: JobKind
    deadline: str = Field(min_length=1)
    spec_digest: str = Field(min_length=1)

    @field_validator("deadline", "spec_digest")
    @classmethod
    def _non_blank_fields(cls, v: str, info: ValidationInfo) -> str:
        return require_nonblank(v, field=str(info.field_name))


class JobStatusChangedPayload(_JobPayload):
    status: JobStatus


class JobTimedOutPayload(_JobPayload):
    deadline: str = Field(min_length=1)

    @field_validator("deadline")
    @classmethod
    def _deadline(cls, v: str, info: ValidationInfo) -> str:
        return require_nonblank(v, field=str(info.field_name))


class JobCancelledPayload(_JobPayload):
    pass


class JobCancelFailedPayload(_JobPayload):
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str, info: ValidationInfo) -> str:
        return require_nonblank(v, field=str(info.field_name))
