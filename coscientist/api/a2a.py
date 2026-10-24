"""A2A facade over ``POST /studies`` and journal event stream (DESIGN §6.5).

Thin API wiring: builds ``A2AFacade`` with adapters around ``post_studies`` and
an injected journal reader. Core must not import this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coscientist.api.studies import CreateStudyBody, post_studies
from coscientist.integrations.a2a_facade import (
    A2A_INTEGRATION_NAME,
    A2ACreateTaskRequest,
    A2AFacade,
    A2ATaskCreated,
)
from coscientist.integrations.config import A2AFacadeConfig
from coscientist.integrations.ports import JournalEventView, JournalStreamPort, StudyCreatePort

__all__ = [
    "A2A_INTEGRATION_NAME",
    "A2ACreateTaskRequest",
    "A2AFacade",
    "A2ATaskCreated",
    "PostStudiesCreatePort",
    "StoreJournalStreamPort",
    "build_a2a_facade",
]


class PostStudiesCreatePort:
    """``StudyCreatePort`` adapter over ``post_studies`` (catalog injected)."""

    def __init__(
        self,
        *,
        data_root: Path | str,
        catalog: Any,
    ) -> None:
        self._data_root = Path(data_root)
        self._catalog = catalog

    async def create_study(
        self,
        *,
        profile: str,
        request: str,
        catalog_version: int,
        idempotency_key: str | None,
    ) -> str:
        body = CreateStudyBody(
            profile=profile,
            request=request,
            catalog_version=catalog_version,
        )
        result = await post_studies(
            self._data_root,
            body,
            idempotency_key=idempotency_key,
            catalog=self._catalog,
        )
        study_id = result.get("study_id")
        if not isinstance(study_id, str) or study_id.strip() == "":
            raise RuntimeError("post_studies returned no study_id")
        return study_id


class StoreJournalStreamPort:
    """``JournalStreamPort`` over a callable that opens a store for one study."""

    def __init__(self, open_store: Any) -> None:
        """``open_store(study_id) ->`` object with ``async read_after(seq)`` + ``aclose``/``close``.

        Kept untyped so tests inject fakes without importing core EventStore.
        """
        self._open_store = open_store

    async def read_after(self, study_id: str, seq: int) -> list[JournalEventView]:
        store = await self._open_store(study_id)
        try:
            events = await store.read_after(seq)
        finally:
            close = getattr(store, "aclose", None)
            if close is not None:
                await close()
            else:
                closer = getattr(store, "close", None)
                if closer is not None:
                    result = closer()
                    if hasattr(result, "__await__"):
                        await result
        out: list[JournalEventView] = []
        for ev in events:
            out.append(
                JournalEventView(
                    seq=int(ev.seq),
                    study_id=str(ev.study_id),
                    type=str(ev.type),
                    entity_key=str(ev.entity_key),
                    payload=dict(ev.payload),
                    schema_version=int(ev.schema_version),
                )
            )
        return out


def build_a2a_facade(
    config: A2AFacadeConfig,
    *,
    study_create: StudyCreatePort,
    journal: JournalStreamPort,
) -> A2AFacade:
    """Construct the facade; ``config.mas`` must enable ``a2a`` before use."""
    return A2AFacade(config, study_create=study_create, journal=journal)
