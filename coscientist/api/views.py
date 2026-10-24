"""GET /studies/{id} and GET /studies/{id}/events (DESIGN §9).

Handlers take injected ports already on main — ``StudyService`` and
``EventStore`` (``coscientist.core.events.ports``). No ``ports.py`` changes.
SQLite open / FastAPI routes are follow-ups; contract tests use fakes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import StrictInt

from coscientist.api.errors import AfterSeqRefuse, EventWireRefuse, StudyIdRefuse
from coscientist.core.events.events import EventType, StoredEvent
from coscientist.core.events.ports import EventStore, StudyService
from coscientist.core.execution.domain import Frozen, StudyView


class EventWire(Frozen):
    """Public journal event for ``GET .../events`` and stream consumers."""

    seq: StrictInt
    study_id: str
    type: EventType
    entity_key: str
    idem_key: str
    payload: dict[str, Any]
    ts: datetime
    schema_version: StrictInt
    epoch: StrictInt
    payload_digest: str


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
    """Map a stamped store row to the public wire DTO (seq required)."""
    if event.seq is None:
        raise EventWireRefuse("stored_event_seq_required")
    # bool is a typing.int subclass — reject via exact type, not isinstance.
    if type(event.seq) is not int:
        raise EventWireRefuse(
            f"stored_event_seq_must_be_strict_int_got_{type(event.seq).__name__}"
        )
    if event.seq < 1:
        raise EventWireRefuse(f"stored_event_seq_must_be_positive_got_{event.seq}")
    if not event.payload_digest:
        raise EventWireRefuse("stored_event_payload_digest_required")
    return EventWire(
        seq=event.seq,
        study_id=event.study_id,
        type=event.type,
        entity_key=event.entity_key,
        idem_key=event.idem_key,
        payload=dict(event.payload),
        ts=event.ts,
        schema_version=event.schema_version,
        epoch=event.epoch,
        payload_digest=event.payload_digest,
    )


def get_study(service: StudyService, study_id: object) -> StudyView:
    """``GET /studies/{id}`` → ``StudyView`` via injected ``StudyService``.

    Missing studies: service raises ``coscientist.api.errors.StudyNotFound``.
    Empty / bad ids refuse here before the port is called.
    """
    sid = require_study_id(study_id)
    return service.study_view(sid)


async def list_study_events(
    store: EventStore, *, after: object
) -> list[EventWire]:
    """``GET /studies/{id}/events?after=<seq>`` via injected ``EventStore``.

    Returns events with ``seq > after`` in store order. Does not invent rows.
    """
    after_seq = require_after_seq(after)
    rows = await store.read_after(after_seq)
    return [event_to_wire(row) for row in rows]
