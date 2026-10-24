"""Event types and envelope (DESIGN §3.5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from coscientist.core.events.digest import payload_digest as _payload_digest
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.job_events import JobEventType
from coscientist.core.events.schema_gate import require_writer_schema_version

# Final+Literal member of EventType; cast avoids AnnAssign Constant (audit field_default).
TOOL_RESOLVED_TYPE: Final[Literal["ToolResolved"]] = cast(
    Literal["ToolResolved"], "ToolResolved"
)

# Writer schema version constants (kept for call sites; must match upcasters).
PLAN_EXPANDED_SCHEMA_VERSION = 5
STUDY_CREATED_SCHEMA_VERSION = 14  # G4: require_hypotheses
# Last StudyCreated schema without require_hypotheses (immutable; not SCHEMA_VERSION - 1).
STUDY_CREATED_REQUIRE_HYPOTHESES_SINCE = 13
STUDY_REFUSED_SCHEMA_VERSION = 2  # #41: required nullable catalog_version
PLAN_ACCEPTED_SCHEMA_VERSION = 12
# Last PlanAccepted schema without Need fields (immutable; not SCHEMA_VERSION - 1).
PLAN_ACCEPTED_NEED_SINCE = 11
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
    # M3 G6: coder/alembic jobs (DESIGN §6.4) — single source in job_events.
    JobEventType,
    "ToolResolved",
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
    schema_version: StrictInt


class StoredEvent(UnstampedEvent):
    """Persisted envelope after store stamp / on read."""

    epoch: StrictInt
    payload_digest: str = Field(min_length=1)


# Compat alias — delete after rename; see https://github.com/Naramerk/repo/issues/114
EventEnvelope = UnstampedEvent


def stamp_envelope(unstamped: UnstampedEvent, *, epoch: int) -> StoredEvent:
    """Stamp lease epoch + digest; refuse naive ts; refuse ahead/behind (exact writer)."""
    return _stamp_envelope(unstamped, epoch=epoch, allow_behind=False)


def stamp_fixture_envelope(unstamped: UnstampedEvent, *, epoch: int) -> StoredEvent:
    """Like stamp_envelope; behind-writer schema_version allowed (fixtures)."""
    return _stamp_envelope(unstamped, epoch=epoch, allow_behind=True)


def _stamp_envelope(unstamped: UnstampedEvent, *, epoch: int, allow_behind: bool) -> StoredEvent:
    if type(epoch) is not int or isinstance(epoch, bool):
        raise TypeError(f"epoch must be exact int, got {epoch!r}")
    if unstamped.ts.tzinfo is None:
        raise JournalFormatError(
            f"{unstamped.type}: naive datetime refused: {unstamped.ts!r}",
            expected_store_format=None,
            found_store_format=None,
        )
    digest = _payload_digest(unstamped.payload)
    if not digest:
        raise JournalFormatError(
            f"{unstamped.type}: empty payload_digest",
            expected_store_format=None,
            found_store_format=None,
        )
    writer_v = require_writer_schema_version(unstamped.type)
    version = unstamped.schema_version
    if version > writer_v:
        raise JournalFormatError(
            f"{unstamped.type}: schema_version {version} ahead of writer v{writer_v}",
            expected_store_format=None,
            found_store_format=None,
        )
    if version < writer_v and not allow_behind:
        raise JournalFormatError(
            f"{unstamped.type}: schema_version {version} behind writer v{writer_v}",
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
