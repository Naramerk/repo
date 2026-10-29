"""Local nickname organization, never authentication or research journal state."""
from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from coscientist.api.errors import ApiRefuse
from coscientist.api.journal import read_events, safe_study_component
from coscientist.api.run_binding import recorded_run_binding, recorded_run_bindings
from coscientist.core.events.errors import CoreError

log = logging.getLogger(__name__)


class ProfileRefuse(ApiRefuse):
    def __init__(self, reason: str, status: int) -> None:
        self.status = status
        super().__init__(reason)


def require_profile_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"lp_[0-9a-f]{32}", value) is None:
        raise ProfileRefuse("profile_id_invalid", 400)
    return value


class LocalProfiles:
    def __init__(self, root: Path) -> None:
        self.root = root

    @contextmanager
    def _connection(self, *, write: bool) -> Iterator[sqlite3.Connection | None]:
        path = self.root / "console-profiles.sqlite3"
        if path.is_symlink():
            raise ProfileRefuse("profile_store_unsafe", 503)
        if not write and not path.exists():
            yield None
            return
        try:
            if write:
                self.root.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(path) if write else path.resolve().as_uri() + "?mode=ro",
                                         uri=not write, timeout=5)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise ProfileRefuse("profile_store_version_unsupported", 503)
                if write:
                    connection.executescript("""
                        CREATE TABLE IF NOT EXISTS local_profiles (
                            id TEXT PRIMARY KEY, nickname TEXT NOT NULL,
                            nickname_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
                        CREATE TABLE IF NOT EXISTS profile_studies (
                            study_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES local_profiles(id),
                            origin TEXT NOT NULL CHECK(origin IN ('launch', 'attached')), request_digest TEXT);
                        -- user_version=2 stores also carried a per-profile settings table,
                        -- removed with its feature; drop the dead table on write.
                        DROP TABLE IF EXISTS profile_settings;
                        PRAGMA user_version=2;
                    """)
                with connection:
                    yield connection
            finally:
                connection.close()
        except (sqlite3.Error, OSError) as exc:
            log.warning("local profile storage error", exc_info=True)
            raise ProfileRefuse("profile_store_unavailable", 503) from exc

    def list_profiles(self) -> list[dict[str, str]]:
        with self._connection(write=False) as connection:
            if connection is None:
                return []
            return [dict(row) for row in connection.execute(
                "SELECT id,nickname,created_at FROM local_profiles ORDER BY nickname_key")]

    def get(self, profile_id: object) -> dict[str, str]:
        pid = require_profile_id(profile_id)
        with self._connection(write=False) as connection:
            row = None
            if connection is not None:
                row = connection.execute("SELECT id,nickname,created_at FROM local_profiles WHERE id=?",
                                         (pid,)).fetchone()
            if row is None:
                raise ProfileRefuse("profile_not_found", 404)
            return dict(row)

    def create(self, nickname: str) -> dict[str, str]:
        name = unicodedata.normalize("NFC", re.sub(r"\s+", " ", nickname).strip())
        if not name or len(name) > 64 or "\x00" in name:
            raise ProfileRefuse("profile_nickname_invalid", 400)
        result = {"id": f"lp_{uuid4().hex}", "nickname": name, "created_at": datetime.now(UTC).isoformat()}
        with self._connection(write=True) as connection:
            assert connection is not None
            try:
                connection.execute("INSERT INTO local_profiles VALUES (?,?,?,?)",
                                   (result["id"], name, name.casefold(), result["created_at"]))
            except sqlite3.IntegrityError as exc:
                raise ProfileRefuse("profile_nickname_exists", 409) from exc
        return result

    def _bind(self, profile_id: str, study_id: str, *, origin: str, request_digest: str | None) -> None:
        pid = require_profile_id(profile_id)
        sid = safe_study_component(study_id)
        with self._connection(write=True) as connection:
            assert connection is not None
            if connection.execute("SELECT 1 FROM local_profiles WHERE id=?", (pid,)).fetchone() is None:
                raise ProfileRefuse("profile_not_found", 404)
            connection.execute("INSERT INTO profile_studies VALUES (?,?,?,?) ON CONFLICT(study_id) DO NOTHING",
                               (sid, pid, origin, request_digest))
            row = connection.execute("SELECT profile_id,request_digest FROM profile_studies WHERE study_id=?",
                                     (sid,)).fetchone()
            assert row is not None
            if row["profile_id"] != pid or (request_digest is not None and row["request_digest"] is not None
                                           and row["request_digest"] != request_digest):
                raise ProfileRefuse("profile_study_conflict", 409)

    def bind_launch(self, profile_id: str, study_id: str, request_digest: str) -> None:
        self._bind(profile_id, study_id, origin="launch", request_digest=request_digest)

    def attach(self, profile_id: str, study_id: str) -> dict[str, str]:
        self.get(profile_id)
        sid = safe_study_component(study_id)
        try:
            events = read_events(self.root, sid, 0)
        except (CoreError, sqlite3.Error, ValueError) as exc:
            raise ProfileRefuse("profile_study_invalid", 400) from exc
        if not any(event["type"] == "StudyCreated" for event in events):
            raise ProfileRefuse("profile_study_not_found", 404)
        binding = recorded_run_binding(self.root, sid)
        owner = sid
        if binding is not None:
            owner = binding[0]
        self._bind(profile_id, owner, origin="attached", request_digest=None)
        return {"profile_id": profile_id, "study_id": owner}

    def study_rows(self, profile_id: str, rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
        self.get(profile_id)
        with self._connection(write=False) as connection:
            assert connection is not None
            owners = {row[0] for row in connection.execute(
                "SELECT study_id FROM profile_studies WHERE profile_id=?", (profile_id,))}
        aliases = {owner: owner for owner in owners}
        for owner, active in recorded_run_bindings(self.root):
            if owner in owners:
                aliases[active] = owner
        return [{**dict(row), "profile_id": profile_id, "root_study_id": aliases[str(row["study_id"])]}
                for row in rows if row["study_id"] in aliases]
