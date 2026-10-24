"""Event types and envelope (DESIGN §3.5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from coscientist.core.events.digest import payload_digest as _payload_digest
from coscientist.core.events.errors import JournalFormatError


class UnstampedEvent(BaseModel):
    """Writer envelope: no epoch / payload_digest (store stamps both)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int | None = None
    study_id: str
    # str (not EventType Literal): synthetic upcaster types in tests + write-path
    # writer_schema_version / upcast already refuse unknown production types.
    type: "EventType"
    entity_key: str
    idem_key: str
    payload: dict[str, Any]
    ts: datetime
    schema_version: StrictInt


class StoredEvent(UnstampedEvent):
    """Persisted envelope after store stamp / on read."""

    epoch: StrictInt
    payload_digest: str = Field(min_length=1)


# Compat alias for call sites / tests still naming the writer envelope.
EventEnvelope = UnstampedEvent


def stamp_envelope(unstamped: UnstampedEvent, *, epoch: int) -> StoredEvent:
    """Stamp lease epoch + digest of ``unstamped.payload`` (single compute site).

    ``schema_version`` is always taken from ``writer_schema_version(type)`` so
    writers cannot embed a mismatched literal; the store still refuses reads of
    foreign versions via ``upcast``.
    """
    from coscientist.core.events.upcasters import writer_schema_version

    if type(epoch) is not int:
        raise TypeError(f"epoch must be exact int, got {epoch!r}")
    digest = _payload_digest(unstamped.payload)
    if not digest:
        raise JournalFormatError(f"{unstamped.type}: empty payload_digest")
    # Always stamp the registry writer version (ignore caller literal).
    version = writer_schema_version(unstamped.type)
    return StoredEvent(
        seq=unstamped.seq,
        study_id=unstamped.study_id,
        type=unstamped.type,
        entity_key=unstamped.entity_key,
        idem_key=unstamped.idem_key,
        payload=unstamped.payload,
        ts=unstamped.ts,
        schema_version=version,
        epoch=epoch,
        payload_digest=digest,
    )


# Typed payload helpers used by the machine / executor.
# Envelope schema_version constants live in schema_versions.py (derived from UPCASTERS).

EventType = Literal[
    "StudyCreated",
    "StudyRefused",
    "StudyCancelled",
    "StudyFinalized",
    "TaskCreated",
    "TaskInherited",
    "AttemptStarted",
    "AttemptClosed",
    "RouteReturned",
    "ResultRecorded",
    "TaskDone",
    "PlanAccepted",
    "PlanExpanded",
    "ExpansionRefused",
    "PlanRefused",
    "LoopCapped",
    "LoopCompleted",
    "LoopRefused",
    "HitlOpened",
    "HitlDecided",
    "HitlTimedOut",
    "HitlDeadlineExtended",
    "BudgetDebited",
    "BudgetExceeded",
    "BudgetSharesAdjusted",
    "BudgetSharesRefused",
    "ArtifactRegistered",
    "LateWriteRejected",
    "DowngradeRejected",
    "CatalogErrorRaised",
    "SandboxInfraRaised",
    "SameDigestRetryRejected",
    "LeaseAcquired",
    "ResultPending",
    "ReviewScheduled",
    "ReviewRecorded",
    "JudgeCasCorrupt",
    "ForkCreated",
    "VerdictSnapshotsRecorded",
    "GraphNodeUpserted",
    "GraphEdgeAdded",
    "EffectIntended",
    "EffectDone",
    "SideEffectDenied",
    "SourceFetched",
    "AttemptTainted",
    "FetchIntegrityRaised",
    "QuestionDisposed",
    "QuestionDisposeRejected",
    # M1 PR20: agent-proposed catalog meta-check (DESIGN_M1 §16).
    "CheckProposed",
    "CheckApproved",
    "CheckRejected",
]


def make_idem(event_type: str, entity_key: str, *parts: str) -> str:
    if parts:
        return f"{event_type}:{entity_key}:{':'.join(parts)}"
    return f"{event_type}:{entity_key}"
