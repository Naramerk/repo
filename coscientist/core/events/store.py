"""SQLite WAL EventStore: lease fencing, idempotency, projections (DESIGN §3.5/§3.5a).

Writes under RLock via to_thread; reads use a separate WAL-safe connection.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from coscientist.core.events import store_db as _db
from coscientist.core.events.clock import Clock, SystemClock
from coscientist.core.events.digest import payload_digest
from coscientist.core.events.errors import (
    AttemptClosedError,
    Conflict,
    Idempotent,
    JournalFormatError,
    JournalLockedError,
    JournalUnavailableError,
    LeaseLost,
    StudyClosedError,
)
from coscientist.core.events.events import StoredEvent, UnstampedEvent, stamp_envelope
from coscientist.core.events.metrics import bump_late_write_rejected
from coscientist.core.events.upcasters import (
    require_exact_int,
    upcast,
    writer_schema_version,
)

_iso = _db.iso
_parse_iso = _db.parse_iso
_open_conn = _db.open_conn
_new_owner_nonce = _db.new_owner_nonce
init_schema = _db.init_schema
ensure_journal_store_format = _db.ensure_journal_store_format
JOURNAL_STORE_FORMAT = _db.JOURNAL_STORE_FORMAT

# Re-export for callers / tests.
__all__ = [
    "SqliteEventStore",
    "JournalFormatError",
    "JournalLockedError",
    "JournalUnavailableError",
    "JOURNAL_STORE_FORMAT",
]

T = TypeVar("T")

# Platform lease TTL (DESIGN §3.5); not a Study domain default.
LEASE_TTL_S = 30.0


def _map_sqlite_open_error(exc: sqlite3.Error) -> Exception:
    """Map sqlite open failures by ``exc.sqlite_errorcode`` (never by message text).

    - ``SQLITE_BUSY`` / ``SQLITE_LOCKED`` → ``JournalLockedError``
    - ``SQLITE_CORRUPT`` / ``SQLITE_NOTADB`` → ``JournalFormatError`` (truncated/corrupt)
    - any other / missing attribute → ``JournalUnavailableError`` (never "truncated")
    """
    try:
        code = exc.sqlite_errorcode
    except AttributeError:
        return JournalUnavailableError(f"journal unavailable: {exc}")  # audit-ok[except_silent_return]: typed refuse/None on expected parse/IO class  # noqa: E501
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return JournalLockedError(f"journal locked: {exc}")
    if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        return JournalFormatError(f"truncated or unreadable journal: {exc}")
    return JournalUnavailableError(f"journal unavailable: {exc}")


class SqliteEventStore:
    """One DB per study. Writes need ``(owner, epoch, owner_nonce)`` fencing."""

    def __init__(
        self,
        study_id: str,
        db_path: Path | str,
        clock: Clock | None = None,
        *,
        owner: str | None = None,
        epoch: int | None = None,
        owner_nonce: str | None = None,
        lease_ttl_s: float,
    ) -> None:
        self.study_id = study_id
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()  # audit-ok[or_call]: coalesce existing call result; no invented literal
        self.owner = owner
        self.owner_nonce = owner_nonce
        # None = no lease held (after release / before acquire). Not a writeable epoch.
        self.epoch: int | None = epoch
        if type(lease_ttl_s) is bool or not isinstance(lease_ttl_s, (int, float)) or lease_ttl_s <= 0:
            raise ValueError(f"lease_ttl_s must be positive number, got {lease_ttl_s!r}")
        self.lease_ttl_s = float(lease_ttl_s)  # audit-ok[coerce_float]: coerce after explicit numeric type gate
        self._closed = False
        # None=unknown; True only after StudyFinalized commit; clear on acquire.
        self._finalized_cached: bool | None = None
        self._db_lock = threading.RLock()
        self._read_lock = threading.RLock()
        with self._db_lock:
            conn: sqlite3.Connection | None = None
            try:
                conn = _open_conn(self.db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA busy_timeout=5000")
                ensure_journal_store_format(conn)
                self._conn = conn
                self._ensure_lease_nonce_column()
                self._conn.commit()
            except JournalFormatError:
                if conn is not None:
                    conn.close()
                raise
            except sqlite3.Error as e:
                if conn is not None:
                    conn.close()
                raise _map_sqlite_open_error(e) from e
            except Exception:
                if conn is not None:
                    conn.close()
                raise  # audit-ok[broad_except]: cleanup then re-raise
        try:
            self._read_conn = _open_conn(self.db_path, readonly=True)
        except sqlite3.Error as e:
            self._conn.close()
            raise _map_sqlite_open_error(e) from e

    def _ensure_lease_nonce_column(self) -> None:
        cols = set()
        for r in self._conn.execute("PRAGMA table_info(lease)").fetchall():
            name = r[1]
            if not isinstance(name, str):
                raise JournalFormatError(
                    f"PRAGMA table_info name must be str, got {type(name).__name__}"
                )
            cols.add(name)
        if "owner_nonce" not in cols:
            # Pre-baseline journals without owner_nonce are refused (no DEFAULT '').
            raise JournalFormatError(
                "lease table missing owner_nonce (pre-baseline journal unsupported)"
            )

    async def _to_thread(self, fn: Callable[[], T]) -> T:
        def _run() -> T:
            with self._db_lock:
                return fn()

        return await asyncio.to_thread(_run)

    async def _read_thread(self, fn: Callable[[], T]) -> T:
        """Off-loop read under ``_read_lock`` (does not take the write lock)."""

        def _run() -> T:
            with self._read_lock:
                if self._closed:
                    raise RuntimeError("store is closed")
                return fn()

        return await asyncio.to_thread(_run)

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        """Close connections after in-flight reads finish. Safe to call more than once."""
        if self._closed:
            return

        def _close() -> None:
            # Take read lock first so we wait for in-flight readers, then mark closed.
            with self._read_lock:
                self._closed = True
                self._read_conn.close()
            with self._db_lock:
                self._conn.close()

        await asyncio.to_thread(_close)

    async def acquire_lease(self, owner: str, ttl_s: float | None = None) -> int:
        """Acquire/renew lease; same owner name + different nonce → LeaseLost while live."""
        ttl = self.lease_ttl_s if ttl_s is None else ttl_s  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
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
                epoch = 1  # audit-ok[literal_assign]: lease/bootstrap literal required by store protocol
                nonce = self.owner_nonce or _new_owner_nonce()  # audit-ok[or_call]: coalesce existing call result; no invented literal  # noqa: E501
                self._conn.execute(
                    "INSERT INTO lease(study_id, owner, owner_nonce, epoch, "
                    "expires_at) VALUES (?,?,?,?,?)",
                    (self.study_id, owner, nonce, epoch, _iso(expires)),
                )
            else:
                exp = _parse_iso(row["expires_at"])
                raw_nonce = row["owner_nonce"]
                if raw_nonce is None:
                    raise JournalFormatError("lease.owner_nonce is NULL")
                if not isinstance(raw_nonce, str):
                    raise JournalFormatError(
                        f"lease.owner_nonce must be str, got {type(raw_nonce).__name__}"
                    )
                if not raw_nonce:
                    raise JournalFormatError(
                        "lease.owner_nonce empty (pre-baseline journal unsupported)"
                    )
                held_nonce = raw_nonce
                if exp > now and row["owner"] != owner:
                    raise LeaseLost(
                        f"lease held by {row['owner']} until {row['expires_at']}"
                    )
                if exp > now and row["owner"] == owner:
                    nonce = self.owner_nonce or _new_owner_nonce()  # audit-ok[or_call]: coalesce existing call result; no invented literal  # noqa: E501
                    if held_nonce != nonce:
                        raise LeaseLost(
                            "lease held by same owner name with different nonce"
                        )
                    epoch = require_exact_int(row["epoch"], what="lease.epoch")
                    self._conn.execute(
                        "UPDATE lease SET expires_at=? WHERE study_id=?",
                        (_iso(expires), self.study_id),
                    )
                else:
                    epoch = require_exact_int(row["epoch"], what="lease.epoch") + 1
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
            self._finalized_cached = None  # peer may have finalized
            return epoch
        except Exception:
            self._conn.rollback()
            raise  # audit-ok[broad_except]: cleanup then re-raise; does not swallow

    async def renew_lease(
        self,
        owner: str,
        epoch: int,
        ttl_s: float | None = None,
        *,
        nonce: str | None = None,
    ) -> None:
        ttl = self.lease_ttl_s if ttl_s is None else ttl_s  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
        nonce = self.owner_nonce if nonce is None else nonce  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
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
        nonce = self.owner_nonce if nonce is None else nonce  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
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
            self.epoch = None

    async def current_lease(self) -> tuple[str | None, int, datetime | None]:
        def _read() -> tuple[str | None, int, datetime | None]:
            cur = self._read_conn.execute(
                "SELECT owner, epoch, expires_at FROM lease WHERE study_id=?",
                (self.study_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None, 0, None
            return row["owner"], require_exact_int(row["epoch"], what="lease.epoch"), _parse_iso(row["expires_at"])

        return await self._read_thread(_read)

    def _fence(self, ttl_s: float | None = None) -> None:
        """Renew lease under the current write transaction.

        The ``UPDATE lease …`` is what serializes peer writers: SQLite takes a
        reserved/write lock on the whole database for the surrounding
        transaction, so a second connection's fence waits until we
        commit/rollback. ``BEGIN IMMEDIATE`` on the append paths is only a
        defensive early lock so the finalized probe cannot race before this
        UPDATE runs.
        """
        if self.owner is None or not self.owner_nonce:
            raise LeaseLost("no lease owner set")
        if self.epoch is None:
            raise LeaseLost("no lease epoch set")
        ttl = self.lease_ttl_s if ttl_s is None else ttl_s  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
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

    def _require_write_epoch(self) -> int:
        if self.epoch is None:
            raise LeaseLost("no lease epoch set")
        return self.epoch

    async def append(self, event: UnstampedEvent) -> StoredEvent:
        return await self._to_thread(lambda: self._append_locked(event))

    def _study_is_finalized(self) -> bool:
        """Return whether the study has a StudyFinalized in projections.

        Only ``True`` is cached. Caching ``False`` is unsafe: a same-batch
        ``StudyFinalized`` (or peer) can flip projections before the next
        append check, and a stale False would skip I3.
        """
        if self._finalized_cached is True:
            return True
        row = self._get_row_write("meta", self.study_id)
        if row and row.get("finalized"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
            self._finalized_cached = True
            return True
        study = self._get_row_write("studies", self.study_id)
        if study and study.get("finalized"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
            self._finalized_cached = True
            return True
        return False

    def _ensure_append_allowed(
        self,
        event: UnstampedEvent,
        *,
        digest: str,
        batch_finalized: bool | None = None,
    ) -> None:
        """I3: after StudyFinalized, reject writes — except identical idempotent retry.

        Idempotency is checked *before* StudyClosedError for every event type so
        an honest retry of an already-recorded row (same idem_key+digest) returns
        the original instead of failing closed. A missing key, or same key with a
        different digest, is rejected (StudyClosedError / Conflict).

        ``digest`` is the stamped digest (from ``stamp_envelope``), not a
        separate recompute — stamped digest is the authority for the incoming
        conflict check.

        ``batch_finalized`` covers an earlier StudyFinalized in the same
        ``append_many`` transaction (projections may not be visible via cache yet).
        """
        if batch_finalized is None:
            batch_flag = False
        else:
            batch_flag = batch_finalized
        finalized = batch_flag or self._study_is_finalized()  # audit-ok[or_call]: coalesce existing call result; no invented literal  # noqa: E501
        if not finalized:
            return
        existing = self._conn.execute(
            "SELECT seq, payload_digest FROM events WHERE idem_key=?",
            (event.idem_key,),
        ).fetchone()
        if existing is not None:
            if existing["payload_digest"] == digest:
                # Identical retry: fall through to _insert_one → Idempotent / existing.
                return
            raise Conflict(
                f"idem_key={event.idem_key} payload mismatch "
                f"(stored={existing['payload_digest']}, new={digest})"
            )
        bump_late_write_rejected(event.type)
        raise StudyClosedError(self.study_id, event.type)

    def _append_locked(self, event: UnstampedEvent) -> StoredEvent:
        mutated = False
        try:
            # Defensive: take the write lock before fence/probe. Peer serialization
            # itself is the lease UPDATE inside ``_fence`` (see that docstring).
            self._conn.execute("BEGIN IMMEDIATE")
            self._fence()
            stamped = stamp_envelope(event, epoch=self._require_write_epoch())
            self._ensure_append_allowed(event, digest=stamped.payload_digest)
            stored, idem = self._insert_one(stamped)
            mutated = True
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            # Insert may have applied finalized projection in-txn; drop cache.
            if mutated:
                self._finalized_cached = None
            raise  # audit-ok[broad_except]: cleanup then re-raise; does not swallow
        if idem is not None:
            raise Idempotent(idem.seq)
        if stored is None:
            raise RuntimeError("append produced no stored event")
        if event.type == "StudyFinalized":
            self._finalized_cached = True
        return stored

    async def append_many(
        self,
        events: list[UnstampedEvent],
        *,
        require_open_attempt: str | None = None,
        _source_fetched_cap_id: str | None = None,
    ) -> list[StoredEvent]:
        return await self._to_thread(
            lambda: self._append_many_sync(
                events,
                require_open_attempt=require_open_attempt,
                source_fetched_cap_id=_source_fetched_cap_id,
            )
        )

    def _fetch_by_idem(self, idem_key: str) -> StoredEvent:
        cur = self._conn.execute(
            "SELECT * FROM events WHERE idem_key=?", (idem_key,)
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(idem_key)
        return self._row_to_event(row)

    def _append_many_sync(
        self,
        events: list[UnstampedEvent],
        *,
        require_open_attempt: str | None = None,
        source_fetched_cap_id: str | None = None,
    ) -> list[StoredEvent]:
        out = []
        mutated = False
        write_epoch = self._require_write_epoch()
        try:
            # Defensive early write lock; ``_fence`` UPDATE is the real serializer.
            self._conn.execute("BEGIN IMMEDIATE")
            self._fence()
            # Probe finalized only after fence so lease ownership is confirmed.
            # In-batch flag: first StudyFinalized closes the rest of the batch
            # even before commit / before _finalized_cached flips to True.
            batch_finalized = self._study_is_finalized()
            if require_open_attempt is not None:
                row = self._get_row_write("attempts", require_open_attempt)
                if row is None or row.get("closed"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_call]: coalesce existing call result; no invented literal  # audit-ok[or_call]: coalesce existing call result; no invented literal  # noqa: E501
                    raise AttemptClosedError(require_open_attempt)
            for ev in events:
                stamped = stamp_envelope(ev, epoch=write_epoch)
                # First StudyFinalized is allowed; after that only identical retry.
                self._ensure_append_allowed(
                    ev, digest=stamped.payload_digest, batch_finalized=batch_finalized
                )
                if ev.type == "SourceFetched":
                    # Must present a capability id whose write permit was granted
                    # by effects.verify (consume). Bool bypass flags are gone.
                    from coscientist.core.events.errors import ArtifactRejected
                    from coscientist.core.execution.source_fetch import (
                        take_source_fetched_write_permit,
                    )

                    if not source_fetched_cap_id:
                        raise ArtifactRejected("fetch_capability_required")
                    if not isinstance(ev.payload, dict):
                        raise JournalFormatError(
                            f"SourceFetched: payload must be dict, "
                            f"got {type(ev.payload).__name__}"
                        )
                    p = ev.payload
                    try:
                        tool = p["tool"]
                        call_id = p["call_id"]
                    except KeyError as e:
                        raise JournalFormatError(
                            f"SourceFetched: missing {e.args[0]!r}"
                        ) from e
                    if not isinstance(tool, str) or not tool:
                        raise JournalFormatError(
                            f"SourceFetched: tool must be non-empty str, got {tool!r}"
                        )
                    if not isinstance(call_id, str) or not call_id:
                        raise JournalFormatError(
                            f"SourceFetched: call_id must be non-empty str, "
                            f"got {call_id!r}"
                        )
                    if not isinstance(ev.study_id, str) or not ev.study_id:
                        raise JournalFormatError(
                            f"SourceFetched: study_id must be non-empty str, "
                            f"got {ev.study_id!r}"
                        )
                    take_source_fetched_write_permit(
                        source_fetched_cap_id,
                        tool=tool,
                        call_id=call_id,
                        study_id=ev.study_id,
                    )
                stored, idem = self._insert_one(stamped)
                mutated = True
                if idem is not None:
                    out.append(self._fetch_by_idem(ev.idem_key))
                    if ev.type == "StudyFinalized":
                        batch_finalized = True
                    continue
                if stored is None:
                    raise RuntimeError("append_many produced no stored event")
                if ev.type == "StudyFinalized":
                    batch_finalized = True
                out.append(stored)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            # Rolled-back insert may have applied finalized projection in-txn.
            if mutated:
                self._finalized_cached = None
            raise  # audit-ok[broad_except]: cleanup then re-raise; does not swallow
        if batch_finalized:
            self._finalized_cached = True
        return out

    def _insert_one(
        self, stamped: StoredEvent
    ) -> tuple[StoredEvent | None, Idempotent | None]:
        """Persist a already-stamped envelope; stamped digest is conflict authority."""
        digest = stamped.payload_digest
        # Empty digest is refused by StoredEvent (Field min_length=1) / stamp_event;
        # this path never sees "".
        write_epoch = self._require_write_epoch()
        if stamped.epoch != write_epoch:
            raise JournalFormatError(
                f"{stamped.type}: epoch mismatch "
                f"(got {stamped.epoch}, store {write_epoch})"
            )
        version = require_exact_int(
            stamped.schema_version, what=f"{stamped.type}.schema_version"
        )
        expected = writer_schema_version(stamped.type)
        if version != expected:
            raise JournalFormatError(
                f"{stamped.type}: write schema_version {version} != writer {expected}"
            )
        cur = self._conn.execute(
            "SELECT seq, payload_digest FROM events WHERE idem_key=?",
            (stamped.idem_key,),
        )
        existing = cur.fetchone()
        if existing is not None:
            if existing["payload_digest"] == digest:
                return None, Idempotent(require_exact_int(existing["seq"], what="events.seq"))
            raise Conflict(
                f"idem_key={stamped.idem_key} payload mismatch "
                f"(stored={existing['payload_digest']}, new={digest})"
            )
        if stamped.ts.tzinfo is None:
            raise JournalFormatError(
                f"{stamped.type}: naive timestamp refused "
                f"(store_format={JOURNAL_STORE_FORMAT})",
                store_format=JOURNAL_STORE_FORMAT,
            )
        ts = stamped.ts
        cur = self._conn.execute(
            "INSERT INTO events(study_id, type, entity_key, idem_key, payload, ts, "
            "schema_version, epoch, payload_digest) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                stamped.study_id,
                stamped.type,
                stamped.entity_key,
                stamped.idem_key,
                json.dumps(stamped.payload, sort_keys=True),
                _iso(ts),
                version,
                write_epoch,
                digest,
            ),
        )
        if cur.lastrowid is None:
            raise JournalUnavailableError("events insert returned no lastrowid")
        seq = require_exact_int(cur.lastrowid, what="events.lastrowid")
        stored = stamped.model_copy(update={"seq": seq, "ts": ts})
        self._apply_projection(stored)
        return stored, None

    async def read_all(self) -> list[StoredEvent]:
        return await self.read_after(0)

    async def read_after(self, seq: int) -> list[StoredEvent]:
        def _read() -> list[StoredEvent]:
            cur = self._read_conn.execute(
                "SELECT * FROM events WHERE study_id=? AND seq>? ORDER BY seq",
                (self.study_id, seq),
            )
            return [self._row_to_event(r) for r in cur.fetchall()]

        return await self._read_thread(_read)

    def _row_to_event(self, row: sqlite3.Row) -> StoredEvent:
        raw_payload = json.loads(row["payload"])
        stored_digest = row["payload_digest"]
        # Old / tampered journals: refuse empty digest or mismatch (never recompute-as-trust).
        if not isinstance(stored_digest, str) or not stored_digest:
            raise JournalFormatError(
                f"{row['type']}: journal row missing payload_digest"
            )
        computed = payload_digest(raw_payload)
        if stored_digest != computed:
            raise JournalFormatError(
                f"{row['type']}: payload_digest mismatch on read "
                f"(stored={stored_digest}, computed={computed})"
            )
        raw_sv = row["schema_version"]
        # SQLite INT affinity may still surface non-ints; never coerce via int().
        version, payload = upcast(row["type"], raw_sv, raw_payload)
        epoch = require_exact_int(row["epoch"], what=f"{row['type']}.epoch")
        return StoredEvent(
            seq=require_exact_int(row["seq"], what="events.seq"),
            study_id=row["study_id"],
            type=row["type"],
            entity_key=row["entity_key"],
            idem_key=row["idem_key"],
            payload=payload,
            ts=_parse_iso(row["ts"]),
            schema_version=version,
            epoch=epoch,
            payload_digest=stored_digest,
        )

    def _get_projection_write(self, table: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT data FROM projections WHERE table_name=?", (table,)
        )
        return [json.loads(r["data"]) for r in cur.fetchall()]

    def _get_row_write(self, table: str, key: str) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "SELECT data FROM projections WHERE table_name=? AND row_key=?",
            (table, key),
        )
        row = cur.fetchone()
        return json.loads(row["data"]) if row else None  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501

    def _upsert_projection_write(
        self, table: str, key: str, data: dict[str, Any]
    ) -> None:
        self._conn.execute(
            "INSERT INTO projections(table_name, row_key, data) VALUES (?,?,?) "
            "ON CONFLICT(table_name, row_key) DO UPDATE SET data=excluded.data",
            (table, key, json.dumps(data, sort_keys=True)),
        )

    async def get_projection(self, table: str) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            cur = self._read_conn.execute(
                "SELECT data FROM projections WHERE table_name=?", (table,)
            )
            return [json.loads(r["data"]) for r in cur.fetchall()]

        return await self._read_thread(_read)

    async def get_row(self, table: str, key: str) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            cur = self._read_conn.execute(
                "SELECT data FROM projections WHERE table_name=? AND row_key=?",
                (table, key),
            )
            row = cur.fetchone()
            return json.loads(row["data"]) if row else None  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501

        return await self._read_thread(_read)

    async def upsert_projection(
        self, table: str, key: str, data: dict[str, Any]
    ) -> None:
        """Fenced projection write via the writer connection."""

        def _write() -> None:
            try:
                self._fence()
                self._upsert_projection_write(table, key, data)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise  # audit-ok[broad_except]: cleanup then re-raise; does not swallow

        await self._to_thread(_write)

    def _apply_projection(self, event: StoredEvent) -> None:
        from coscientist.core.events.projections import apply_event

        apply_event(self, event)
