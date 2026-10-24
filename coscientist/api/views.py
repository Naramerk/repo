"""GET /studies/{id} and GET /studies/{id}/events (DESIGN §9).

Handlers take injected ``StudyService`` / ``EventStore``
(``coscientist.core.events.ports``). ``StudyNotFound`` is the core port type.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StrictInt, ValidationError

from coscientist.api.errors import (
    AfterSeqRefuse,
    EventWireRefuse,
    StudyIdRefuse,
    StudyStoreMismatch,
)
from coscientist.core.events.events import EventType, StoredEvent
from coscientist.core.events.ports import EventStore, StudyService
from coscientist.core.execution.domain import Frozen, StudyView


class EventWire(Frozen):
    """Public journal event for ``GET .../events`` and stream consumers."""

    seq: Annotated[StrictInt, Field(ge=1)]
    study_id: str
    type: EventType
    entity_key: str
    idem_key: str
    payload: dict[str, Any]
    ts: datetime
    schema_version: StrictInt
    epoch: StrictInt
    payload_digest: Annotated[str, Field(min_length=1)]


def require_study_id(study_id: object) -> str:
    """Fail-closed study_id gate (non-str / empty / surrounding whitespace)."""
    if not isinstance(study_id, str):
        raise StudyIdRefuse(
            f"study_id must be str, got {type(study_id).__name__}"
        )
    if not study_id or study_id != study_id.strip():
        raise StudyIdRefuse("study_id_empty_or_whitespace")
    return study_id


def require_after_seq(after: object) -> int:
    """``after`` must be a non-negative StrictInt (bool refused)."""
    if isinstance(after, bool) or type(after) is not int:
        raise AfterSeqRefuse(
            "after_must_be_strict_int_got_"
            f"{type(after).__name__}:{after!r}"
        )
    if after < 0:
        raise AfterSeqRefuse(f"after_must_be_non_negative_got_{after}")
    return after


def event_to_wire(event: StoredEvent) -> EventWire:
    """Map a stamped store row to the public wire DTO (single validate path)."""
    try:
        return EventWire.model_validate(event.model_dump())
    except ValidationError as exc:
        raise EventWireRefuse(f"event_wire_validate:{exc.error_count()}") from exc


def get_study(service: StudyService, study_id: object) -> StudyView:
    """``GET /studies/{id}`` → ``StudyView`` via injected ``StudyService``.

    Missing studies: service raises ``StudyNotFound`` (core port).
    Empty / bad ids refuse here before the port is called.
    """
    sid = require_study_id(study_id)
    return service.study_view(sid)


async def list_study_events(
    store: EventStore,
    *,
    study_id: object,
    after: object,
) -> list[EventWire]:
    """``GET /studies/{id}/events?after=<seq>`` via injected ``EventStore``.

    ``study_id`` must match ``store.study_id`` and every ``EventWire.study_id``.
    """
    sid = require_study_id(study_id)
    if store.study_id != sid:
        raise StudyStoreMismatch(
            f"store_study_id_mismatch:path={sid!r}:store={store.study_id!r}"
        )
    after_seq = require_after_seq(after)
    rows = await store.read_after(after_seq)
    wires: list[EventWire] = []
    for row in rows:
        wire = event_to_wire(row)
        if wire.study_id != sid:
            raise StudyStoreMismatch(
                f"event_study_id_mismatch:path={sid!r}:event={wire.study_id!r}:seq={wire.seq}"
            )
        wires.append(wire)
    return wires
