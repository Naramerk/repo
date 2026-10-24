"""Event types and envelope (DESIGN §3.5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from coscientist.core.events.digest import payload_digest as _payload_digest
from coscientist.core.events.errors import JournalFormatError

# Wire default when writers omit schema_version; make_unstamped_event stamps from registry.
SCHEMA_VERSION_WIRE_DEFAULT = 1

# Writer schema version constants (must equal writer_schema_version; production = v1).
PLAN_EXPANDED_SCHEMA_VERSION = 1
STUDY_CREATED_SCHEMA_VERSION = 1
STUDY_REFUSED_SCHEMA_VERSION = 1
PLAN_ACCEPTED_SCHEMA_VERSION = 1
TASK_CREATED_SCHEMA_VERSION = 1
STUDY_FINALIZED_SCHEMA_VERSION = 1

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
    # Default when callers omit the kwarg; prefer make_unstamped_event which stamps
    # require_writer_schema_version(type) so writers never pass literal ints.
    schema_version: StrictInt = (  # audit-ok[named_default]: wire default; make_unstamped_event stamps from registry
        SCHEMA_VERSION_WIRE_DEFAULT
    )


class StoredEvent(UnstampedEvent):
    """Persisted envelope after store stamp / on read."""

    epoch: StrictInt
    payload_digest: str = Field(min_length=1)




def make_unstamped_event(
    *,
    study_id: str,
    type: EventType,
    entity_key: str,
    idem_key: str,
    payload: dict[str, Any],
    ts: datetime,
    seq: int | None = None,
) -> UnstampedEvent:
    """Build an ``UnstampedEvent`` stamped with the registry writer schema version.

    Writers must not pass ``schema_version`` literals; this helper is the
    single construction path that reads ``require_writer_schema_version``.
    """
    from coscientist.core.events.schema_gate import require_writer_schema_version

    return UnstampedEvent(
        seq=seq,
        study_id=study_id,
        type=type,
        entity_key=entity_key,
        idem_key=idem_key,
        payload=payload,
        ts=ts,
        schema_version=require_writer_schema_version(type),
    )


def stamp_envelope(unstamped: UnstampedEvent, *, epoch: int) -> StoredEvent:
    """Stamp lease epoch + digest of ``unstamped.payload`` (single compute site).

    Preserves the writer's ``schema_version`` (needed for legacy-journal fixtures
    and intentional old-version seeds). Refuses versions ahead of the current
    writer line; reads still chain through ``upcast_strict``.
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
    writer_v = require_writer_schema_version(unstamped.type)
    version = int(unstamped.schema_version)
    if version > writer_v:
        raise JournalFormatError(
            f"{unstamped.type}: schema_version {version} ahead of writer v{writer_v}",
            expected_store_format=None,
            found_store_format=None,
        )
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
