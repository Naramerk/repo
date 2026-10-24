"""Event types and envelope (DESIGN §3.5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from coscientist.core.events.digest import payload_digest as _payload_digest
from coscientist.core.events.errors import JournalFormatError

# Writer schema version constants (kept for call sites; must match upcasters).
PLAN_EXPANDED_SCHEMA_VERSION = 5
STUDY_CREATED_SCHEMA_VERSION = 13  # #41: catalog_version StrictInt
STUDY_REFUSED_SCHEMA_VERSION = 2  # #41: required nullable catalog_version
PLAN_ACCEPTED_SCHEMA_VERSION = 11
TASK_CREATED_SCHEMA_VERSION = 14
STUDY_FINALIZED_SCHEMA_VERSION = 4  # PR20: custom_checks / observations

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


class UnstampedEvent(BaseModel):
    """Writer envelope: no epoch / payload_digest (store stamps both)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int | None = None  # audit-ok[field_default]: UnstampedEvent.seq is None until store assigns lastrowid
    study_id: str
    type: EventType
    entity_key: str
    idem_key: str
    payload: dict[str, Any]
    ts: datetime
    # Default 1 for legacy writers that omit the kwarg; store re-stamps via
    # writer_schema_version(type) so this literal never becomes the persisted version.
    schema_version: StrictInt = 1  # audit-ok[field_default]: UnstampedEvent.schema_version default 1; store re-stamps from writer_schema_version(type) audit-ok[number_outside_domain]: schema_version default 1 overwritten by stamp_envelope


class StoredEvent(UnstampedEvent):
    """Persisted envelope after store stamp / on read."""

    epoch: StrictInt
    payload_digest: str = Field(min_length=1)


# Compat alias: writers still construct ``EventEnvelope(...)`` without stamp fields.
EventEnvelope = UnstampedEvent


def stamp_envelope(unstamped: UnstampedEvent, *, epoch: int) -> StoredEvent:
    """Stamp lease epoch + digest of ``unstamped.payload`` (single compute site).

    ``schema_version`` is taken from ``writer_schema_version(type)`` so writers
    cannot embed a mismatched literal; reads still refuse foreign versions via
    ``upcast``.
    """
    from coscientist.core.events.schema_gate import require_writer_schema_version

    if type(epoch) is not int or isinstance(epoch, bool):
        raise TypeError(f"epoch must be exact int, got {epoch!r}")
    digest = _payload_digest(unstamped.payload)
    if not digest:
        raise JournalFormatError(
            f"{unstamped.type}: empty payload_digest",
            expected_store_format=None,
            found_store_format=None,
        )
    version = require_writer_schema_version(unstamped.type)
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


def make_idem(event_type: str, entity_key: str, *parts: str) -> str:
    rest = ":".join(parts)
    return f"{event_type}:{entity_key}" + (f":{rest}" if rest else "")
