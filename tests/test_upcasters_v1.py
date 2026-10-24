"""Schema v1 baseline: upcaster mechanism + store-format gate (no production chains)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.store_db import JOURNAL_STORE_FORMAT, parse_store_format_marker
from coscientist.core.events.upcasters import (
    UPCASTERS,
    assert_upcaster_chain_continuous,
    map_spec_dict_list,
    register_upcaster,
    require_exact_int,
    upcast,
    writer_schema_version,
)


@pytest.fixture
def dummy_upcasters():
    """Register a continuous chain on a synthetic type; remove afterward."""
    name = "_DummySchemaV1"
    UPCASTERS.pop(name, None)

    def v1_to_v2(payload: dict) -> dict:
        out = dict(payload)
        if "src" in out:
            out["dst"] = out["src"]
        return out

    def v2_to_v3(payload: dict) -> dict:
        return dict(payload)

    register_upcaster(name, 1, v1_to_v2)
    register_upcaster(name, 2, v2_to_v3)
    assert_upcaster_chain_continuous(name, writer_schema_version(name))
    try:
        yield name
    finally:
        UPCASTERS.pop(name, None)


def test_register_upcaster_refuses_overwrite(dummy_upcasters):
    name = dummy_upcasters
    with pytest.raises(ValueError, match="duplicate upcaster"):
        register_upcaster(name, 1, lambda p: p)


def test_dummy_chain_continuous_and_upcast(dummy_upcasters):
    name = dummy_upcasters
    assert writer_schema_version(name) == 3
    assert_upcaster_chain_continuous(name, 3)
    v, data = upcast(name, 1, {"src": "exact"})
    assert v == 3
    assert data == {"src": "exact", "dst": "exact"}
    v2, data2 = upcast(name, 3, {"src": "x"})
    assert v2 == 3
    assert data2 == {"src": "x"}


def test_upcast_raises_unknown_type():
    with pytest.raises(JournalFormatError, match="unknown event type"):
        upcast("NotAnEventTypeEver", 1, {})


def test_upcast_raises_above_writer():
    assert writer_schema_version("StudyCreated") == 1
    with pytest.raises(JournalFormatError, match="above writer"):
        upcast("StudyCreated", 2, {})


@pytest.mark.parametrize("bad", [0, -1])
def test_upcast_raises_non_positive_version(bad):
    with pytest.raises(JournalFormatError, match="schema_version must be >= 1"):
        upcast("StudyCreated", bad, {})


@pytest.mark.parametrize("bad", ["1", True, False, 1.0, 1.9])
def test_upcast_raises_non_exact_int_version(bad):
    with pytest.raises(JournalFormatError, match="must be exact int"):
        upcast("StudyCreated", bad, {})


def test_require_exact_int_rejects_bool():
    with pytest.raises(JournalFormatError, match="exact int"):
        require_exact_int(True, what="x")


def test_upcast_raises_chain_gap(dummy_upcasters):
    name = dummy_upcasters
    del UPCASTERS[name][1]
    assert writer_schema_version(name) == 3
    with pytest.raises(JournalFormatError, match="chain gap"):
        upcast(name, 1, {"src": "a"})


def test_assert_upcaster_chain_continuous_detects_gap(dummy_upcasters):
    name = dummy_upcasters
    del UPCASTERS[name][1]
    with pytest.raises(AssertionError, match="gaps"):
        assert_upcaster_chain_continuous(name, 3)


def test_production_types_are_writer_v1():
    for et in (
        "StudyCreated",
        "TaskCreated",
        "PlanAccepted",
        "PlanExpanded",
        "AttemptStarted",
        "StudyFinalized",
        "ResultRecorded",
        "TaskDone",
        "AttemptClosed",
    ):
        assert writer_schema_version(et) == 1
        assert et not in UPCASTERS or not UPCASTERS[et]
        v, data = upcast(et, 1, {"k": 1})
        assert v == 1
        assert data == {"k": 1}


def test_map_spec_dict_list_maps_inputs_and_outputs():
    spec = {
        "inputs": [{"id": "a"}, "skip", {"id": "b"}],
        "expected_outputs": [{"name": "o"}],
        "other": 1,
    }

    def add_mark(row: dict) -> dict:
        row = dict(row)
        row["marked"] = True
        return row

    out = map_spec_dict_list(spec, "inputs", add_mark)
    assert out["inputs"] == [
        {"id": "a", "marked": True},
        "skip",
        {"id": "b", "marked": True},
    ]
    assert out["expected_outputs"] == [{"name": "o"}]
    assert out["other"] == 1
    outs = map_spec_dict_list(out, "expected_outputs", add_mark)
    assert outs["expected_outputs"] == [{"name": "o", "marked": True}]


def test_event_envelope_rejects_bool_schema_version():
    with pytest.raises(ValidationError):
        EventEnvelope(
            study_id="s",
            type="StudyCreated",
            entity_key="s",
            idem_key="k",
            payload={},
            ts=datetime(2024, 1, 1, tzinfo=UTC),
            schema_version=True,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("raw", [" 1", "01", "+1", "1.0", "true", ""])
def test_store_format_marker_strict_parse(raw):
    with pytest.raises(JournalFormatError):
        parse_store_format_marker(raw)


def test_store_format_marker_accepts_exact_text():
    assert parse_store_format_marker(str(JOURNAL_STORE_FORMAT)) == JOURNAL_STORE_FORMAT


@pytest.mark.asyncio
async def test_store_refuses_old_journal_without_store_format(tmp_path: Path):
    db = tmp_path / "events.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE events(
          seq INTEGER PRIMARY KEY, study_id TEXT NOT NULL, type TEXT NOT NULL,
          entity_key TEXT NOT NULL, idem_key TEXT NOT NULL UNIQUE, payload JSON NOT NULL,
          ts TEXT NOT NULL, schema_version INT NOT NULL, epoch INT NOT NULL,
          payload_digest TEXT NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO events VALUES (1,'s','StudyCreated','s','k','{}',"
        "'2024-01-01T00:00:00+00:00',1,0,'d')"
    )
    conn.commit()
    conn.close()
    with pytest.raises(JournalFormatError, match="store_format"):
        SqliteEventStore("s", db)


@pytest.mark.asyncio
async def test_store_refuses_forged_store_format_marker(tmp_path: Path):
    db = tmp_path / "events.db"
    store = SqliteEventStore("s", db)
    await store.close()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE journal_meta SET value=? WHERE key='store_format'", ("01",)
    )
    conn.commit()
    conn.close()
    with pytest.raises(JournalFormatError, match="store_format"):
        SqliteEventStore("s", db)


@pytest.mark.asyncio
async def test_store_refuses_non_numeric_store_format(tmp_path: Path):
    db = tmp_path / "events.db"
    store = SqliteEventStore("s", db)
    await store.close()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE journal_meta SET value=? WHERE key='store_format'", ("nope",)
    )
    conn.commit()
    conn.close()
    with pytest.raises(JournalFormatError, match="store_format"):
        SqliteEventStore("s", db)


@pytest.mark.asyncio
async def test_new_store_writes_store_format_marker(tmp_path: Path):
    db = tmp_path / "events.db"
    store = SqliteEventStore("s", db)
    try:
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT value FROM journal_meta WHERE key='store_format'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == str(JOURNAL_STORE_FORMAT)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_write_rejects_schema_version_not_equal_writer(tmp_path: Path):
    store = SqliteEventStore("s", tmp_path / "e.db")
    await store.acquire_lease("owner")
    try:
        with pytest.raises(JournalFormatError, match="!= writer"):
            # Bypass StrictInt by mutating after construct is impossible (frozen);
            # insert via raw SQL path is covered below — here use model_construct.
            ev = EventEnvelope.model_construct(
                study_id="s",
                type="StudyCreated",
                entity_key="s",
                idem_key=make_idem("StudyCreated", "s"),
                payload={"study_id": "s"},
                ts=datetime(2024, 1, 1, tzinfo=UTC),
                schema_version=2,
                epoch=0,
                payload_digest="",
            )
            await store.append(ev)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_read_rejects_string_schema_version_in_db(tmp_path: Path):
    db = tmp_path / "e.db"
    store = SqliteEventStore("s", db)
    await store.close()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO events(study_id, type, entity_key, idem_key, payload, ts, "
        "schema_version, epoch, payload_digest) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "s",
            "StudyCreated",
            "s",
            "StudyCreated:s",
            "{}",
            "2024-01-01T00:00:00+00:00",
            # Non-numeric text survives INT affinity; "1" would coerce to integer.
            "not-an-int",
            0,
            "d",
        ),
    )
    conn.commit()
    conn.close()
    store2 = SqliteEventStore("s", db)
    try:
        with pytest.raises(JournalFormatError, match="exact int"):
            await store2.read_all()
    finally:
        await store2.close()


@pytest.mark.asyncio
async def test_read_accepts_mixed_valid_schema_versions(tmp_path: Path, dummy_upcasters):
    """Both stored versions are valid for their types (dummy writer=3 @v2 + StudyCreated @v1)."""
    name = dummy_upcasters
    db = tmp_path / "e.db"
    store = SqliteEventStore("s", db)
    await store.close()
    conn = sqlite3.connect(str(db))
    rows = [
        ("s", "StudyCreated", "s", "StudyCreated:s", "{}", 1),
        ("s", name, "s/d", f"{name}:s/d", '{"src":"x"}', 2),
    ]
    for study_id, typ, ek, ik, payload, sv in rows:
        conn.execute(
            "INSERT INTO events(study_id, type, entity_key, idem_key, payload, ts, "
            "schema_version, epoch, payload_digest) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                study_id,
                typ,
                ek,
                ik,
                payload,
                "2024-01-01T00:00:00+00:00",
                sv,
                0,
                "d",
            ),
        )
    conn.commit()
    conn.close()
    store2 = SqliteEventStore("s", db)
    try:
        events = await store2.read_all()
        assert len(events) == 2
        by_type = {e.type: e for e in events}
        assert by_type["StudyCreated"].schema_version == 1
        assert by_type[name].schema_version == 3  # upcasted from stored v2
        assert by_type[name].payload.get("src") == "x"
    finally:
        await store2.close()


def test_upcast_rejects_schema_version_above_writer_huge():
    """schema_version 2**63 (exact int, far above writer) → JournalFormatError."""
    with pytest.raises(JournalFormatError, match="above writer"):
        upcast("StudyCreated", 2**63, {})


@pytest.mark.asyncio
async def test_truncated_db_refused(tmp_path: Path):
    db = tmp_path / "e.db"
    store = SqliteEventStore("s", db)
    await store.close()
    # Truncate the file so sqlite cannot open a coherent journal.
    db.write_bytes(db.read_bytes()[:32])
    with pytest.raises(JournalFormatError, match="truncated|unreadable"):
        SqliteEventStore("s", db)


def test_reclassified_fixture_matches_generator():
    from scripts.gen_reclassified_fetch_old_journal import check_committed

    assert check_committed() == 0


@pytest.mark.asyncio
async def test_attempt_started_missing_attempt_role_raises(tmp_path: Path):
    from coscientist.core.execution.local_project import LocalProjectMixin

    class _RT(LocalProjectMixin):
        def __init__(self) -> None:
            self.attempts = {}
            self.tasks = {
                "t1": type(
                    "T",
                    (),
                    {
                        "attempts": [],
                        "model_copy": lambda self, update: self,
                        "spec": type("S", (), {"task_key": "E1"})(),
                    },
                )()
            }

    rt = _RT()
    ev = EventEnvelope(
        study_id="s",
        type="AttemptStarted",
        entity_key="a1",
        idem_key="AttemptStarted:a1",
        payload={
            "attempt_id": "a1",
            "task_id": "t1",
            "task_key": "E1",
            "inputs": [],
            "tainted_context": False,
        },
        ts=datetime(2024, 1, 1, tzinfo=UTC),
        schema_version=1,
    )
    with pytest.raises(JournalFormatError, match="attempt_role"):
        rt._project_local(ev)  # noqa: SLF001
