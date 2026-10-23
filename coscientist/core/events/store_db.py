"""SQLite connection helpers for SqliteEventStore (keeps store.py ≤ 500 lines)."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY, study_id TEXT NOT NULL, type TEXT NOT NULL,
  entity_key TEXT NOT NULL, idem_key TEXT NOT NULL UNIQUE, payload JSON NOT NULL,
  ts TEXT NOT NULL, schema_version INT NOT NULL, epoch INT NOT NULL,
  payload_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lease(
  study_id TEXT PRIMARY KEY, owner TEXT NOT NULL, owner_nonce TEXT NOT NULL,
  epoch INT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projections(
  table_name TEXT NOT NULL, row_key TEXT NOT NULL, data JSON NOT NULL,
  PRIMARY KEY (table_name, row_key));
"""


def new_owner_nonce() -> str:
    return str(uuid.uuid4())


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def open_conn(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
