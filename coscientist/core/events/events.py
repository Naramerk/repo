"""Event types and envelope (DESIGN §3.5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictInt

from coscientist.core.events.digest import payload_digest as _payload_digest


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int | None = None
    study_id: str
    type: str
    entity_key: str
    idem_key: str
    payload: dict[str, Any]
    ts: datetime
    schema_version: StrictInt
    # Writers omit these; SqliteEventStore stamps epoch from the lease and
    # payload_digest from the actual payload before persist. After read/write
    # both are always set (non-empty digest). Empty digest in an old journal
    # row is refused — never guessed.
    epoch: StrictInt | None = None
    payload_digest: str | None = None


def stamp_envelope(event: EventEnvelope, *, epoch: int) -> EventEnvelope:
    """Stamp lease epoch + digest of ``event.payload`` (single compute site)."""
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        raise TypeError(f"epoch must be exact int, got {epoch!r}")
    digest = _payload_digest(event.payload)
    if not digest:
        raise ValueError(f"{event.type}: empty payload_digest")
    return event.model_copy(update={"epoch": epoch, "payload_digest": digest})


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
    rest = ":".join(parts)
    return f"{event_type}:{entity_key}" + (f":{rest}" if rest else "")
