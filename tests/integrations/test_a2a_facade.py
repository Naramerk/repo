"""A2A facade: create study + stream journal events (fakes; no network)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.integrations.a2a_facade import (
    A2A_INTEGRATION_NAME,
    A2ACreateTaskRequest,
    A2AFacade,
)
from coscientist.integrations.config import (
    A2AAgentCardConfig,
    A2AFacadeConfig,
    ExternalMasConfig,
)
from coscientist.integrations.errors import IntegrationDisabled
from coscientist.integrations.ports import JournalEventView


def _card() -> A2AAgentCardConfig:
    return A2AAgentCardConfig(
        name="coscientist",
        description="research study facade",
        url="https://example.test/a2a",
        version="0.1.0",
        protocol_version="0.2.1",
        default_input_modes=("text",),
        default_output_modes=("text",),
        skills=("create_study",),
    )


def _cfg(*, enabled: bool) -> A2AFacadeConfig:
    names = (A2A_INTEGRATION_NAME,) if enabled else ()
    return A2AFacadeConfig(
        mas=ExternalMasConfig(enabled_integrations=names),
        agent_card=_card(),
        study_profile="research",
        catalog_version=3,
    )


class _FakeCreate:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_study(
        self,
        *,
        profile: str,
        request: str,
        catalog_version: int,
        idempotency_key: str | None,
    ) -> str:
        self.calls.append(
            {
                "profile": profile,
                "request": request,
                "catalog_version": catalog_version,
                "idempotency_key": idempotency_key,
            }
        )
        return "st_fake_1"


class _FakeJournal:
    def __init__(self, events: list[JournalEventView]) -> None:
        self._events = events
        self.calls: list[tuple[str, int]] = []

    async def read_after(self, study_id: str, seq: int) -> list[JournalEventView]:
        self.calls.append((study_id, seq))
        return [e for e in self._events if e.study_id == study_id and e.seq > seq]


def test_agent_card_refuses_when_disabled() -> None:
    facade = A2AFacade(_cfg(enabled=False), study_create=_FakeCreate(), journal=_FakeJournal([]))
    with pytest.raises(IntegrationDisabled) as ei:
        facade.agent_card()
    assert ei.value.name == A2A_INTEGRATION_NAME


def test_agent_card_from_config() -> None:
    facade = A2AFacade(_cfg(enabled=True), study_create=_FakeCreate(), journal=_FakeJournal([]))
    card = facade.agent_card()
    assert card["name"] == "coscientist"
    assert card["protocolVersion"] == "0.2.1"
    assert card["skills"] == ["create_study"]


@pytest.mark.asyncio
async def test_create_task_maps_to_study_create() -> None:
    create = _FakeCreate()
    facade = A2AFacade(_cfg(enabled=True), study_create=create, journal=_FakeJournal([]))
    created = await facade.create_task(
        A2ACreateTaskRequest(message="find inhibitors", idempotency_key="k1")
    )
    assert created.task_id == "st_fake_1"
    assert created.study_id == "st_fake_1"
    assert create.calls == [
        {
            "profile": "research",
            "request": "find inhibitors",
            "catalog_version": 3,
            "idempotency_key": "k1",
        }
    ]


@pytest.mark.asyncio
async def test_create_task_refuses_when_disabled() -> None:
    facade = A2AFacade(_cfg(enabled=False), study_create=_FakeCreate(), journal=_FakeJournal([]))
    with pytest.raises(IntegrationDisabled):
        await facade.create_task(A2ACreateTaskRequest(message="x"))


@pytest.mark.asyncio
async def test_stream_events_yields_journal_after_seq() -> None:
    events = [
        JournalEventView(
            seq=1,
            study_id="st_fake_1",
            type="StudyCreated",
            entity_key="st_fake_1",
            payload={"request": "q"},
            schema_version=13,
        ),
        JournalEventView(
            seq=2,
            study_id="st_fake_1",
            type="TaskCreated",
            entity_key="t1",
            payload={},
            schema_version=14,
        ),
    ]
    journal = _FakeJournal(events)
    facade = A2AFacade(_cfg(enabled=True), study_create=_FakeCreate(), journal=journal)
    streamed = [e async for e in facade.stream_events("st_fake_1", after_seq=1)]
    assert [e.seq for e in streamed] == [2]
    assert journal.calls == [("st_fake_1", 1)]


@pytest.mark.asyncio
async def test_list_events_rejects_non_int_after_seq() -> None:
    facade = A2AFacade(_cfg(enabled=True), study_create=_FakeCreate(), journal=_FakeJournal([]))
    with pytest.raises(TypeError):
        await facade.list_events("st_fake_1", after_seq=True)  # type: ignore[arg-type]


def test_create_task_request_rejects_blank_message() -> None:
    with pytest.raises(ValidationError):
        A2ACreateTaskRequest(message="  ")


def test_facade_config_requires_strict_catalog_version() -> None:
    with pytest.raises(ValidationError):
        A2AFacadeConfig(
            mas=ExternalMasConfig(enabled_integrations=(A2A_INTEGRATION_NAME,)),
            agent_card=_card(),
            study_profile="research",
            catalog_version=True,  # type: ignore[arg-type]
        )
