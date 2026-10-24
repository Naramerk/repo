"""Event types and envelope (DESIGN §3.5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int | None = None
    study_id: str
    type: str
    entity_key: str
    idem_key: str
    payload: dict[str, Any]
    ts: datetime
    schema_version: int = 1
    epoch: int = 0
    payload_digest: str = ""


# Typed payload helpers used by the machine / executor.

# Writer: #24 TC11 / #29 PA10+PE4 / #28 TC13/PA11/PE5 / #23 StudyCreated v11;
# PR20 stacks StudyCreated **v12** (max_proposals).
PLAN_EXPANDED_SCHEMA_VERSION = 5
STUDY_CREATED_SCHEMA_VERSION = 12  # PR20: max_proposals (#23 owns v11)
PLAN_ACCEPTED_SCHEMA_VERSION = 11
TASK_CREATED_SCHEMA_VERSION = 13
STUDY_FINALIZED_SCHEMA_VERSION = 4  # PR20: custom_checks / observations
# Executor writers (match upcasters.writer_schema_version; #38 owns schema_versions.py).
RESULT_RECORDED_SCHEMA_VERSION = 3
ATTEMPT_CLOSED_SCHEMA_VERSION = 2
TASK_DONE_SCHEMA_VERSION = 2

EventType = Literal[
    "StudyCreated",
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
    "BudgetSharesAdjusted",
    "BudgetSharesRefused",
    "ArtifactRegistered",
    "LateWriteRejected",
    "CatalogErrorRaised",
    "SandboxInfraRaised",
    "SameDigestRetryRejected",
    "LeaseAcquired",
    "ResultPending",
    "ReviewScheduled",
    "ReviewRecorded",
    "PlanRefused",
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
