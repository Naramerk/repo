"""API A2A wiring adapters over post_studies + fake journal store."""

from __future__ import annotations

from pathlib import Path

import pytest

from coscientist.api.a2a import (
    PostStudiesCreatePort,
    StoreJournalStreamPort,
    build_a2a_facade,
)
from coscientist.integrations.a2a_facade import A2ACreateTaskRequest
from coscientist.integrations.config import (
    A2AAgentCardConfig,
    A2AFacadeConfig,
    ExternalMasConfig,
)
from tests.checks.minimal_test_catalog import minimal_test_catalog


class _Ev:
    def __init__(self, seq: int, study_id: str) -> None:
        self.seq = seq
        self.study_id = study_id
        self.type = "StudyCreated"
        self.entity_key = study_id
        self.payload = {"request": "q"}
        self.schema_version = 13


class _Store:
    def __init__(self, study_id: str) -> None:
        self.study_id = study_id
        self.closed = False

    async def read_after(self, seq: int) -> list[_Ev]:
        return [_Ev(1, self.study_id)] if 1 > seq else []

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_post_studies_create_port_and_stream(tmp_path: Path) -> None:
    catalog = minimal_test_catalog()
    create = PostStudiesCreatePort(data_root=tmp_path, catalog=catalog)
    opened: list[str] = []

    async def open_store(study_id: str) -> _Store:
        opened.append(study_id)
        return _Store(study_id)

    journal = StoreJournalStreamPort(open_store)
    cfg = A2AFacadeConfig(
        mas=ExternalMasConfig(enabled_integrations=("a2a",)),
        agent_card=A2AAgentCardConfig(
            name="coscientist",
            description="study facade",
            url="https://example.test/a2a",
            version="0.1.0",
            protocol_version="0.2.1",
            default_input_modes=("text",),
            default_output_modes=("text",),
            skills=("create_study",),
        ),
        study_profile="research",
        catalog_version=3,
    )
    facade = build_a2a_facade(cfg, study_create=create, journal=journal)
    created = await facade.create_task(A2ACreateTaskRequest(message="wire test"))
    assert created.study_id.startswith("st_")
    events = [e async for e in facade.stream_events(created.study_id, after_seq=0)]
    assert len(events) == 1
    assert events[0].type == "StudyCreated"
    assert opened == [created.study_id]
