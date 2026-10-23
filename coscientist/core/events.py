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

# Writer constants (#21 PlanExpanded v2 / TaskCreated v10 / PlanAccepted v8;
# this PR stacks TaskCreated v11 + PlanAccepted v9 + PlanExpanded v3 loop fields.
# StudyCreated v10 (max_loop_iter) stays here — #24 merges before #23 (v11).
PLAN_EXPANDED_SCHEMA_VERSION = 3
STUDY_CREATED_SCHEMA_VERSION = 10
PLAN_ACCEPTED_SCHEMA_VERSION = 9
TASK_CREATED_SCHEMA_VERSION = 11

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
]


def make_idem(event_type: str, entity_key: str, *parts: str) -> str:
    rest = ":".join(parts)
    return f"{event_type}:{entity_key}" + (f":{rest}" if rest else "")
