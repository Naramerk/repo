"""SQLite connection helpers for SqliteEventStore (keeps store.py ≤ 500 lines)."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from coscientist.core.events.errors import JournalFormatError

# Pre-release schema v1 baseline. Journals without this marker (old dev DBs) are
# refused so reopen cannot collide on payload_digest after envelope changes.
JOURNAL_STORE_FORMAT = 1
_STORE_FORMAT_KEY = "store_format"

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
CREATE TABLE IF NOT EXISTS journal_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL);
"""


def _is_strict_int_text(raw: str) -> bool:
    """True for ``0`` or ``[1-9][0-9]*`` — reject spaces, padding, signs, floats."""
    if raw == "0":
        return True
    if not raw or raw[0] == "0":
        return False
    for ch in raw:
        if not ("0" <= ch <= "9"):
            return False
    return True


def new_owner_nonce() -> str:
    return str(uuid.uuid4())


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise JournalFormatError(
            f"naive datetime refused (store_format={JOURNAL_STORE_FORMAT}): {dt!r}",
            expected_store_format=JOURNAL_STORE_FORMAT,
            found_store_format=None,
        )
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


def parse_store_format_marker(raw: object) -> int:
    """Parse ``journal_meta.store_format``: exact decimal text, no spaces/padding."""
    if type(raw) is not str:
        raise JournalFormatError(
            f"journal store_format must be str, got {type(raw).__name__}: {raw!r}",
            expected_store_format=JOURNAL_STORE_FORMAT,
            found_store_format=None,
        )
    if not _is_strict_int_text(raw):
        raise JournalFormatError(
            f"journal store_format is not a strict int text: {raw!r}",
            expected_store_format=JOURNAL_STORE_FORMAT,
            found_store_format=None,
        )
    n = 0
    for ch in raw:
        n = n * 10 + (ord(ch) - 48)
    return n


def ensure_journal_store_format(conn: sqlite3.Connection) -> None:
    """Require ``journal_meta.store_format == JOURNAL_STORE_FORMAT``.

    Empty DBs (no events) receive the marker. Existing events without a matching
    marker are refused — old pre-baseline journals must not be reopened.
    """
    init_schema(conn)
    row = conn.execute(
        "SELECT value FROM journal_meta WHERE key = ?", (_STORE_FORMAT_KEY,)
    ).fetchone()
    if row is None:
        has_events = conn.execute("SELECT 1 FROM events LIMIT 1").fetchone()
        if has_events is not None:
            raise JournalFormatError(
                "journal lacks store_format marker (pre-schema-v1 baseline); "
                "refusing to open",
                expected_store_format=JOURNAL_STORE_FORMAT,
                found_store_format=None,
            )
        conn.execute(
            "INSERT INTO journal_meta(key, value) VALUES (?, ?)",
            (_STORE_FORMAT_KEY, str(JOURNAL_STORE_FORMAT)),
        )
        return
    raw = row["value"] if isinstance(row, sqlite3.Row) else row[0]
    found = parse_store_format_marker(raw)
    if found != JOURNAL_STORE_FORMAT:
        raise JournalFormatError(
            f"journal store_format={found} incompatible with "
            f"JOURNAL_STORE_FORMAT={JOURNAL_STORE_FORMAT}",
            expected_store_format=JOURNAL_STORE_FORMAT,
            found_store_format=found,
        )
