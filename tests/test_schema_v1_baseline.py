"""Schema v1 baseline: Unstamped/Stored, EventType, store_format, typed upcast refuse."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.digest import payload_digest
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import (
    EventEnvelope,
    StoredEvent,
    UnstampedEvent,
    stamp_envelope,
)
from coscientist.core.events.schema_gate import (
    require_writer_schema_version,
    upcast_strict,
)
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.store_db import (
    JOURNAL_STORE_FORMAT,
    ensure_journal_store_format,
    init_schema,
    open_conn,
    parse_store_format_marker,
)
from coscientist.core.events.upcasters import writer_schema_version


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


def _unstamped(clock: FakeClock, **overrides: object) -> UnstampedEvent:
    base = dict(
        study_id="st1",
        type="StudyCreated",
        entity_key="st1",
        idem_key="StudyCreated:st1",
        payload={"study_id": "st1"},
        ts=clock.now(),
        schema_version=writer_schema_version("StudyCreated"),
    )
    base.update(overrides)
    return UnstampedEvent(**base)  # type: ignore[arg-type]


def test_unstamped_event_rejects_epoch_and_digest_fields(clock: FakeClock) -> None:
    with pytest.raises(ValidationError):
        UnstampedEvent(  # type: ignore[call-arg]
            study_id="st1",
            type="StudyCreated",
            entity_key="st1",
            idem_key="k",
            payload={},
            ts=clock.now(),
            schema_version=writer_schema_version("StudyCreated"),
            epoch=0,
        )
    with pytest.raises(ValidationError):
        UnstampedEvent(  # type: ignore[call-arg]
            study_id="st1",
            type="StudyCreated",
            entity_key="st1",
            idem_key="k",
            payload={},
            ts=clock.now(),
            schema_version=writer_schema_version("StudyCreated"),
            payload_digest="",
        )


def test_event_envelope_alias_is_unstamped() -> None:
    assert EventEnvelope is UnstampedEvent
    assert issubclass(StoredEvent, UnstampedEvent)


def test_envelope_type_must_be_event_type(clock: FakeClock) -> None:
    with pytest.raises(ValidationError):
        UnstampedEvent(
            study_id="st1",
            type="NotARealEventType",  # type: ignore[arg-type]
            entity_key="st1",
            idem_key="k",
            payload={},
            ts=clock.now(),
            schema_version=1,
        )


def test_stamp_envelope_sets_epoch_and_digest(clock: FakeClock) -> None:
    u = _unstamped(clock, schema_version=writer_schema_version("StudyCreated"))
    stored = stamp_envelope(u, epoch=3)
    assert isinstance(stored, StoredEvent)
    assert stored.epoch == 3
    assert stored.payload_digest == payload_digest(u.payload)
    assert stored.schema_version == u.schema_version


def test_unstamped_event_requires_schema_version(clock: FakeClock) -> None:
    with pytest.raises(ValidationError):
        UnstampedEvent(  # type: ignore[call-arg]
            study_id="st1",
            type="StudyCreated",
            entity_key="st1",
            idem_key="k",
            payload={},
            ts=clock.now(),
        )


def test_stamp_envelope_refuses_schema_version_behind_writer(clock: FakeClock) -> None:
    writer_v = writer_schema_version("StudyCreated")
    assert writer_v > 1
    u = _unstamped(clock, schema_version=1)
    with pytest.raises(JournalFormatError, match="behind writer") as ei:
        stamp_envelope(u, epoch=1)
    assert "StudyCreated" in str(ei.value)


def test_stamp_envelope_accepts_exact_writer_schema_version(clock: FakeClock) -> None:
    writer_v = writer_schema_version("StudyCreated")
    u = _unstamped(clock, schema_version=writer_v)
    stored = stamp_envelope(u, epoch=1)
    assert stored.schema_version == writer_v


@pytest.mark.asyncio
async def test_store_stamps_digest_writers_cannot_supply(
    tmp_path: Path, clock: FakeClock
) -> None:
    store = SqliteEventStore("st1", tmp_path / "s.db", clock=clock)
    await store.acquire_lease("o1")
    # Current writer version + fields required by the live StudyCreated chain.
    payload = {
        "study_id": "st1",
        "catalog_version": 1,
        "max_loop_iter": 8,
        "max_proposals": 3,
    }
    ev = _unstamped(
        clock,
        payload=payload,
        schema_version=writer_schema_version("StudyCreated"),
    )
    out = await store.append(ev)
    assert out.epoch == store.epoch
    assert out.payload_digest == payload_digest(payload)
    assert len(out.payload_digest) >= 1
    rows = await store.read_all()
    assert rows[0].payload_digest == out.payload_digest
    await store.close()


@pytest.mark.asyncio
async def test_new_store_writes_store_format_marker(
    tmp_path: Path, clock: FakeClock
) -> None:
    path = tmp_path / "m.db"
    store = SqliteEventStore("st1", path, clock=clock)
    await store.close()
    conn = open_conn(path)
    row = conn.execute(
        "SELECT value FROM journal_meta WHERE key='store_format'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert parse_store_format_marker(row[0]) == JOURNAL_STORE_FORMAT


@pytest.mark.asyncio
async def test_old_journal_without_store_format_refused(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE events(
          seq INTEGER PRIMARY KEY, study_id TEXT, type TEXT, entity_key TEXT,
          idem_key TEXT UNIQUE, payload JSON, ts TEXT, schema_version INT,
          epoch INT, payload_digest TEXT);
        INSERT INTO events VALUES (
          1,'st1','StudyCreated','st1','k','{}','2026-01-01T00:00:00+00:00',
          1,1,'deadbeef');
        """
    )
    conn.commit()
    conn.close()
    with pytest.raises(JournalFormatError, match="store_format|pre-schema-v1") as ei:
        SqliteEventStore("st1", path)
    assert ei.value.expected_store_format == JOURNAL_STORE_FORMAT
    assert ei.value.found_store_format is None


@pytest.mark.asyncio
async def test_journal_format_error_mismatch_sets_found_store_format(
    tmp_path: Path,
) -> None:
    path = tmp_path / "badver.db"
    conn = open_conn(path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO journal_meta(key, value) VALUES ('store_format', '99')"
    )
    conn.commit()
    conn.close()
    with pytest.raises(JournalFormatError) as ei:
        SqliteEventStore("st1", path)
    assert ei.value.expected_store_format == JOURNAL_STORE_FORMAT
    assert ei.value.found_store_format == 99
    assert ei.value.store_format == JOURNAL_STORE_FORMAT


def test_parse_store_format_marker_rejects_padding() -> None:
    with pytest.raises(JournalFormatError) as ei:
        parse_store_format_marker("01")
    assert ei.value.expected_store_format == JOURNAL_STORE_FORMAT
    assert ei.value.found_store_format is None


def test_upcast_strict_refuses_unknown_event_type() -> None:
    with pytest.raises(JournalFormatError, match="unknown event type") as ei:
        upcast_strict("NotAnEventTypeEver", 1, {})
    assert ei.value.expected_store_format is None


def test_upcast_strict_refuses_version_ahead_of_writer() -> None:
    writer_v = writer_schema_version("StudyCreated")
    with pytest.raises(JournalFormatError, match="ahead of writer") as ei:
        upcast_strict("StudyCreated", writer_v + 1, {"study_id": "s"})
    assert "StudyCreated" in str(ei.value)


def test_require_writer_schema_version_refuses_unknown_type() -> None:
    with pytest.raises(JournalFormatError, match="unknown event type"):
        require_writer_schema_version("DefinitelyNotRegistered")


def test_ensure_empty_db_gets_marker(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    conn = open_conn(path)
    ensure_journal_store_format(conn)
    conn.commit()
    row = conn.execute(
        "SELECT value FROM journal_meta WHERE key='store_format'"
    ).fetchone()
    conn.close()
    assert parse_store_format_marker(row[0]) == JOURNAL_STORE_FORMAT
