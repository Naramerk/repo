"""Inbound A2A facade: create study + stream journal events (DESIGN §6.5).

Depends on ``StudyCreatePort`` / ``JournalStreamPort`` only — no core imports.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from coscientist.integrations.config import A2AFacadeConfig
from coscientist.integrations.errors import IntegrationDisabled
from coscientist.integrations.ports import (
    JournalEventView,
    JournalStreamPort,
    StudyCreatePort,
)

# Integration name that must appear in ExternalMasConfig.enabled_integrations.
A2A_INTEGRATION_NAME = "a2a"


class A2ACreateTaskRequest(BaseModel):
    """A2A task → study create (message text is the research request)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    message: str = Field(min_length=1)
    # Optional wire field: absent → None (caller may omit; never invent a key).
    idempotency_key: str | None = None  # audit-ok[field_default]: optional A2A Idempotency-Key; None means omit

    @field_validator("message")
    @classmethod
    def _message(cls, v: str) -> str:
        if v != v.strip() or v == "":
            raise ValueError("message must be a non-empty stripped string")
        return v


class A2ATaskCreated(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    task_id: str = Field(min_length=1)
    study_id: str = Field(min_length=1)


class A2AFacade:
    """A2A agent card + task create + journal event stream over injected ports."""

    def __init__(
        self,
        config: A2AFacadeConfig,
        *,
        study_create: StudyCreatePort,
        journal: JournalStreamPort,
    ) -> None:
        self._config = config
        self._study_create = study_create
        self._journal = journal

    def agent_card(self) -> dict[str, object]:
        """Return the configured agent card (typed refuse when A2A disabled)."""
        self._require_enabled()
        card = self._config.agent_card
        return {
            "name": card.name,
            "description": card.description,
            "url": card.url,
            "version": card.version,
            "protocolVersion": card.protocol_version,
            "defaultInputModes": list(card.default_input_modes),
            "defaultOutputModes": list(card.default_output_modes),
            "skills": list(card.skills),
        }

    async def create_task(self, request: A2ACreateTaskRequest) -> A2ATaskCreated:
        """Map an A2A task to ``POST /studies`` via ``StudyCreatePort``."""
        self._require_enabled()
        study_id = await self._study_create.create_study(
            profile=self._config.study_profile,
            request=request.message,
            catalog_version=self._config.catalog_version,
            idempotency_key=request.idempotency_key,
        )
        if not isinstance(study_id, str) or study_id.strip() == "":
            raise RuntimeError("StudyCreatePort returned empty study_id")
        return A2ATaskCreated(task_id=study_id, study_id=study_id)

    async def list_events(
        self, task_id: str, *, after_seq: StrictInt | int
    ) -> list[JournalEventView]:
        """Journal events with ``seq > after_seq`` (GET events semantics)."""
        self._require_enabled()
        if type(after_seq) is not int or isinstance(after_seq, bool):
            raise TypeError(f"after_seq must be exact int, got {after_seq!r}")
        if after_seq < 0:
            raise ValueError("after_seq must be >= 0")
        if not isinstance(task_id, str) or task_id.strip() == "":
            raise ValueError("task_id must be a non-empty stripped string")
        return await self._journal.read_after(task_id, after_seq)

    async def stream_events(
        self, task_id: str, *, after_seq: StrictInt | int
    ) -> AsyncIterator[JournalEventView]:
        """Async stream of journal events after ``after_seq`` (one read batch)."""
        events = await self.list_events(task_id, after_seq=after_seq)
        for event in events:
            yield event

    def _require_enabled(self) -> None:
        self._config.mas.requires(A2A_INTEGRATION_NAME)
