"""SQLite WAL EventStore: lease fencing, idempotency, projections (DESIGN §3.5 / §3.5a).

Async write/lease APIs run sqlite under ``threading.RLock`` via ``asyncio.to_thread``.
Payload/projection JSON is strict (no ``_json_default``).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from coscientist.core.clock import Clock, SystemClock
from coscientist.core.digest import payload_digest
from coscientist.core.errors import AttemptClosedError, Conflict, Idempotent, LeaseLost
from coscientist.core.events import EventEnvelope
from coscientist.core.upcasters import upcast

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

T = TypeVar("T")


def _new_owner_nonce() -> str:
    return str(uuid.uuid4())


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class SqliteEventStore:
    """One DB per study. Writes need ``(owner, epoch, owner_nonce)`` fencing.

    Prefer ``acquire_lease``. Seeded owner/epoch without nonce cannot write.
    """

    def __init__(
        self,
        study_id: str,
        db_path: Path | str,
        clock: Clock | None = None,
        *,
        owner: str | None = None,
        epoch: int = 0,
        owner_nonce: str | None = None,
        lease_ttl_s: float = 30.0,
    ) -> None:
        self.study_id = study_id
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()
        self.owner = owner
        self.owner_nonce = owner_nonce
        self.epoch = epoch
        self.lease_ttl_s = lease_ttl_s
        self._closed = False
        # Test hooks: sleep inside a write txn (off the event loop) and signal entry.
        self._write_delay_s: float = 0.0
        self._write_delay_entered: threading.Event | None = None
        self._db_lock = threading.RLock()
        with self._db_lock:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._ensure_lease_nonce_column()
            self._conn.commit()

    def _ensure_lease_nonce_column(self) -> None:
        cols = {
            str(r[1]) for r in self._conn.execute("PRAGMA table_info(lease)").fetchall()
        }
        if "owner_nonce" not in cols:
            self._conn.execute(
                "ALTER TABLE lease ADD COLUMN owner_nonce TEXT NOT NULL DEFAULT ''"
            )

    async def _to_thread(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` under the DB lock on a worker thread (does not block the loop)."""

        def _run() -> T:
            with self._db_lock:
                return fn()

        return await asyncio.to_thread(_run)

    def _with_lock(self, fn: Callable[[], T]) -> T:
        with self._db_lock:
            return fn()

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close the underlying connection. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True

        def _close() -> None:
            self._conn.close()

        self._with_lock(_close)

    async def acquire_lease(self, owner: str, ttl_s: float | None = None) -> int:
        """Acquire/renew lease; same owner name + different nonce → LeaseLost while live."""
        ttl = self.lease_ttl_s if ttl_s is None else ttl_s
        return await self._to_thread(lambda: self._acquire_lease_locked(owner, ttl))

    def _acquire_lease_locked(self, owner: str, ttl: float) -> int:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            now = self.clock.now()
            expires = now + timedelta(seconds=ttl)
            cur = self._conn.execute(
                "SELECT owner, owner_nonce, epoch, expires_at FROM lease "
                "WHERE study_id=?",
                (self.study_id,),
            )
            row = cur.fetchone()
            if row is None:
                epoch = 1
                nonce = self.owner_nonce or _new_owner_nonce()
                self._conn.execute(
                    "INSERT INTO lease(study_id, owner, owner_nonce, epoch, "
                    "expires_at) VALUES (?,?,?,?,?)",
                    (self.study_id, owner, nonce, epoch, _iso(expires)),
                )
            else:
                exp = _parse_iso(row["expires_at"])
                held_nonce = str(row["owner_nonce"] or "")
                if exp > now and row["owner"] != owner:
                    raise LeaseLost(
                        f"lease held by {row['owner']} until {row['expires_at']}"
                    )
                if exp > now and row["owner"] == owner:
                    # Same owner name: renew only when nonce matches this instance.
                    nonce = self.owner_nonce or _new_owner_nonce()
                    if held_nonce != nonce:
                        raise LeaseLost(
                            "lease held by same owner name with different nonce"
                        )
                    epoch = int(row["epoch"])
                    self._conn.execute(
                        "UPDATE lease SET expires_at=? WHERE study_id=?",
                        (_iso(expires), self.study_id),
                    )
                else:
                    epoch = int(row["epoch"]) + 1
                    nonce = _new_owner_nonce()
                    self._conn.execute(
                        "UPDATE lease SET owner=?, owner_nonce=?, epoch=?, "
                        "expires_at=? WHERE study_id=?",
                        (owner, nonce, epoch, _iso(expires), self.study_id),
                    )
            self._conn.commit()
            self.owner = owner
            self.owner_nonce = nonce
            self.epoch = epoch
            self.lease_ttl_s = ttl
            return epoch
        except Exception:
            self._conn.rollback()
            raise

    async def renew_lease(
        self,
        owner: str,
        epoch: int,
        ttl_s: float | None = None,
        *,
        nonce: str | None = None,
    ) -> None:
        """Extend lease or raise ``LeaseLost`` on missing/mismatched nonce or expiry."""
        ttl = self.lease_ttl_s if ttl_s is None else ttl_s
        nonce = self.owner_nonce if nonce is None else nonce
        if not nonce:
            raise LeaseLost("renew failed: missing owner nonce")
        await self._to_thread(
            lambda: self._renew_lease_locked(owner, epoch, ttl, nonce)
        )

    def _renew_lease_locked(
        self, owner: str, epoch: int, ttl: float, nonce: str
    ) -> None:
        now = self.clock.now()
        expires = now + timedelta(seconds=ttl)
        cur = self._conn.execute(
            "UPDATE lease SET expires_at=? "
            "WHERE study_id=? AND owner=? AND epoch=? AND owner_nonce=? "
            "AND expires_at>?",
            (_iso(expires), self.study_id, owner, epoch, nonce, _iso(now)),
        )
        if cur.rowcount == 0:
            self._conn.rollback()
            raise LeaseLost("renew failed: lease lost or epoch/nonce mismatch")
        self._conn.commit()
        self.lease_ttl_s = ttl

    async def release_lease(
        self,
        owner: str,
        epoch: int,
        *,
        nonce: str | None = None,
    ) -> None:
        """Delete lease row if identity matches; else ``LeaseLost`` (same as renew)."""
        nonce = self.owner_nonce if nonce is None else nonce
        if not nonce:
            raise LeaseLost("release failed: missing owner nonce")
        await self._to_thread(lambda: self._release_lease_locked(owner, epoch, nonce))

    def _release_lease_locked(self, owner: str, epoch: int, nonce: str) -> None:
        cur = self._conn.execute(
            "DELETE FROM lease WHERE study_id=? AND owner=? AND epoch=? "
            "AND owner_nonce=?",
            (self.study_id, owner, epoch, nonce),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise LeaseLost("release failed: lease lost or epoch/nonce mismatch")
        if self.owner == owner and self.epoch == epoch:
            self.owner = None
            self.owner_nonce = None
            self.epoch = 0

    def current_lease(self) -> tuple[str | None, int, datetime | None]:
        def _read() -> tuple[str | None, int, datetime | None]:
            cur = self._conn.execute(
                "SELECT owner, epoch, expires_at FROM lease WHERE study_id=?",
                (self.study_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None, 0, None
            return row["owner"], int(row["epoch"]), _parse_iso(row["expires_at"])

        return self._with_lock(_read)

    def _fence(self, ttl_s: float | None = None) -> None:
        if self.owner is None or not self.owner_nonce:
            raise LeaseLost("no lease owner set")
        ttl = self.lease_ttl_s if ttl_s is None else ttl_s
        now = self.clock.now()
        cur = self._conn.execute(
            "UPDATE lease SET expires_at=? "
            "WHERE study_id=? AND owner=? AND epoch=? AND owner_nonce=? "
            "AND expires_at>?",
            (
                _iso(now + timedelta(seconds=ttl)),
                self.study_id,
                self.owner,
                self.epoch,
                self.owner_nonce,
                _iso(now),
            ),
        )
        if cur.rowcount == 0:
            raise LeaseLost("epoch fencing rejected write")

    async def append(self, event: EventEnvelope) -> EventEnvelope:
        """Append one event. Idempotent replay raises Idempotent(stored_seq)."""
        return await self._to_thread(lambda: self._append_locked(event))

    def _append_locked(self, event: EventEnvelope) -> EventEnvelope:
        try:
            self._maybe_write_delay()
            self._fence()
            stored, idem = self._insert_one(event)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        if idem is not None:
            raise Idempotent(idem.seq)
        assert stored is not None
        return stored

    async def append_many(
        self,
        events: list[EventEnvelope],
        *,
        require_open_attempt: str | None = None,
    ) -> list[EventEnvelope]:
        return await self._to_thread(
            lambda: self._append_many_sync(
                events, require_open_attempt=require_open_attempt
            )
        )

    def _fetch_by_idem(self, idem_key: str) -> EventEnvelope:
        cur = self._conn.execute(
            "SELECT * FROM events WHERE idem_key=?", (idem_key,)
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(idem_key)
        return self._row_to_event(row)

    def _maybe_write_delay(self) -> None:
        delay = self._write_delay_s
        if delay and delay > 0:
            if self._write_delay_entered is not None:
                self._write_delay_entered.set()
            time.sleep(delay)

    def _append_many_sync(
        self,
        events: list[EventEnvelope],
        *,
        require_open_attempt: str | None = None,
    ) -> list[EventEnvelope]:
        out: list[EventEnvelope] = []
        try:
            self._maybe_write_delay()
            self._fence()
            if require_open_attempt is not None:
                row = self.get_row("attempts", require_open_attempt)
                if row is None or row.get("closed"):
                    raise AttemptClosedError(require_open_attempt)
            for ev in events:
                stored, idem = self._insert_one(ev)
                if idem is not None:
                    out.append(self._fetch_by_idem(ev.idem_key))
                    continue
                assert stored is not None
                out.append(stored)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return out

    def _insert_one(
        self, event: EventEnvelope
    ) -> tuple[EventEnvelope | None, Idempotent | None]:
        digest = event.payload_digest or payload_digest(event.payload)
        cur = self._conn.execute(
            "SELECT seq, payload_digest FROM events WHERE idem_key=?",
            (event.idem_key,),
        )
        existing = cur.fetchone()
        if existing is not None:
            if existing["payload_digest"] == digest:
                return None, Idempotent(int(existing["seq"]))
            raise Conflict(
                f"idem_key={event.idem_key} payload mismatch "
                f"(stored={existing['payload_digest']}, new={digest})"
            )
        ts = event.ts if event.ts.tzinfo else event.ts.replace(tzinfo=UTC)
        cur = self._conn.execute(
            "INSERT INTO events(study_id, type, entity_key, idem_key, payload, ts, "
            "schema_version, epoch, payload_digest) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event.study_id,
                event.type,
                event.entity_key,
                event.idem_key,
                json.dumps(event.payload, sort_keys=True),
                _iso(ts),
                event.schema_version,
                self.epoch,
                digest,
            ),
        )
        seq = int(cur.lastrowid or 0)
        stored = event.model_copy(
            update={"seq": seq, "epoch": self.epoch, "payload_digest": digest, "ts": ts}
        )
        self._apply_projection(stored)
        return stored, None

    def read_all(self) -> list[EventEnvelope]:
        return self.read_after(0)

    def read_after(self, seq: int) -> list[EventEnvelope]:
        def _read() -> list[EventEnvelope]:
            cur = self._conn.execute(
                "SELECT * FROM events WHERE study_id=? AND seq>? ORDER BY seq",
                (self.study_id, seq),
            )
            return [self._row_to_event(r) for r in cur.fetchall()]

        return self._with_lock(_read)

    def _row_to_event(self, row: sqlite3.Row) -> EventEnvelope:
        payload = json.loads(row["payload"])
        version, payload = upcast(row["type"], int(row["schema_version"]), payload)
        return EventEnvelope(
            seq=int(row["seq"]),
            study_id=row["study_id"],
            type=row["type"],
            entity_key=row["entity_key"],
            idem_key=row["idem_key"],
            payload=payload,
            ts=_parse_iso(row["ts"]),
            schema_version=version,
            epoch=int(row["epoch"]),
            payload_digest=row["payload_digest"],
        )

    def get_projection(self, table: str) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            cur = self._conn.execute(
                "SELECT data FROM projections WHERE table_name=?", (table,)
            )
            return [json.loads(r["data"]) for r in cur.fetchall()]

        return self._with_lock(_read)

    def get_row(self, table: str, key: str) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            cur = self._conn.execute(
                "SELECT data FROM projections WHERE table_name=? AND row_key=?",
                (table, key),
            )
            row = cur.fetchone()
            return json.loads(row["data"]) if row else None

        return self._with_lock(_read)

    def upsert_projection(self, table: str, key: str, data: dict[str, Any]) -> None:
        def _write() -> None:
            self._conn.execute(
                "INSERT INTO projections(table_name, row_key, data) VALUES (?,?,?) "
                "ON CONFLICT(table_name, row_key) DO UPDATE SET data=excluded.data",
                (table, key, json.dumps(data, sort_keys=True)),
            )

        self._with_lock(_write)

    def _apply_projection(self, event: EventEnvelope) -> None:
        from coscientist.core.projections import apply_event

        apply_event(self, event)

    def fold_from_events(self) -> dict[str, dict[str, dict[str, Any]]]:
        from coscientist.core.projections import fold_events

        return fold_events(self.read_all())
