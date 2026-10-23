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
    "HitlOpened",
    "HitlDecided",
    "HitlTimedOut",
    "HitlDeadlineExtended",
    "BudgetDebited",
    "ArtifactRegistered",
    "LateWriteRejected",
    "CatalogErrorRaised",
    "SandboxInfraRaised",
    "SameDigestRetryRejected",
    "LeaseAcquired",
    "ReviewScheduled",
    "ForkCreated",
    "VerdictSnapshotsRecorded",
    "GraphNodeUpserted",
    "GraphEdgeAdded",
]


def make_idem(event_type: str, entity_key: str, *parts: str) -> str:
    rest = ":".join(parts)
    return f"{event_type}:{entity_key}" + (f":{rest}" if rest else "")
