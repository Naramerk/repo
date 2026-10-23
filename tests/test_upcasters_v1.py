"""Schema v1 baseline: upcaster mechanism + store-format gate (no production chains)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.store_db import JOURNAL_STORE_FORMAT
from coscientist.core.events.upcasters import (
    UPCASTERS,
    assert_upcaster_chain_continuous,
    map_spec_dict_list,
    register_upcaster,
    upcast,
    writer_schema_version,
)


@pytest.fixture
def dummy_upcasters():
    """Register a continuous chain on a synthetic type; remove afterward."""
    name = "_DummySchemaV1"
    # Ensure clean slate
    UPCASTERS.pop(name, None)

    def v1_to_v2(payload: dict) -> dict:
        out = dict(payload)
        # Provably exact: copy an existing field under a new key (stored bytes → stored bytes).
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
    with pytest.raises(ValueError, match="unknown event type"):
        upcast("NotAnEventTypeEver", 1, {})


def test_upcast_raises_above_writer():
    assert writer_schema_version("StudyCreated") == 1
    with pytest.raises(ValueError, match="above writer"):
        upcast("StudyCreated", 2, {})


def test_upcast_raises_chain_gap(dummy_upcasters):
    name = dummy_upcasters
    # Punch a gap at v1→v2 by removing from_version=1; max key 2 keeps writer=3.
    del UPCASTERS[name][1]
    assert writer_schema_version(name) == 3
    with pytest.raises(ValueError, match="chain gap"):
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
    assert out["inputs"] == [{"id": "a", "marked": True}, "skip", {"id": "b", "marked": True}]
    assert out["expected_outputs"] == [{"name": "o"}]
    assert out["other"] == 1
    outs = map_spec_dict_list(out, "expected_outputs", add_mark)
    assert outs["expected_outputs"] == [{"name": "o", "marked": True}]


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
        assert int(row[0]) == JOURNAL_STORE_FORMAT
    finally:
        await store.close()
