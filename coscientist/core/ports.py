"""Ports fixed for parallel cloud-agent work after M0 (DESIGN §13)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from coscientist.core.domain import Artifact, StudyView
from coscientist.core.events import EventEnvelope


class EventStore(Protocol):
    study_id: str

    async def append(self, event: EventEnvelope) -> EventEnvelope: ...

    async def append_many(self, events: list[EventEnvelope]) -> list[EventEnvelope]: ...

    def read_all(self) -> list[EventEnvelope]: ...

    def read_after(self, seq: int) -> list[EventEnvelope]: ...

    def get_projection(self, table: str) -> list[dict[str, Any]]: ...

    def get_row(self, table: str, key: str) -> dict[str, Any] | None: ...

    async def acquire_lease(self, owner: str, ttl_s: float = 30.0) -> int: ...

    async def renew_lease(
        self,
        owner: str,
        epoch: int,
        ttl_s: float = 30.0,
        *,
        nonce: str | None = None,
    ) -> None: ...

    async def release_lease(
        self,
        owner: str,
        epoch: int,
        *,
        nonce: str | None = None,
    ) -> None: ...

    def current_lease(self) -> tuple[str | None, int, datetime | None]: ...


class ArtifactStore(Protocol):
    def put(
        self, content: bytes, produced_by_call_id: str, *, name: str, media_type: str
    ) -> Artifact: ...

    def read(self, artifact_id: str) -> bytes: ...

    def get_meta(self, artifact_id: str) -> Artifact | None: ...


class AgentPort(Protocol):
    async def run(self, role: str, input: Any, tools: list[Any], budget: Any) -> Any: ...


class StudyService(Protocol):
    def study_view(self, study_id: str) -> StudyView: ...
