"""Pure read-only journal access shared by API contracts and HTTP console."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from coscientist.api.errors import ApiRefuse
from coscientist.api.views import EventWireDict, event_to_wire
from coscientist.core.events.store import stored_event_from_row


def safe_study_component(name: str) -> str:
    """Accept CLI and API journal IDs while refusing path components that escape."""
    if not name or name != name.strip() or name in (".", "..") or any(
        char in name for char in ("/", "\\", "\x00")
    ):
        raise ApiRefuse("study_dir_unsafe")
    return name


def study_path(root: Path, sid: str) -> Path:
    path = root / safe_study_component(sid)
    if path.resolve().parent != root.resolve():
        raise ApiRefuse("study_dir_unsafe")
    return path


@contextmanager
def journal(root: Path, sid: str) -> Iterator[sqlite3.Connection]:
    db = study_path(root, sid) / "events.db"
    if not db.is_file() or db.is_symlink():
        raise ApiRefuse("study_not_found")
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def read_events(root: Path, sid: str, after: int) -> list[EventWireDict]:
    """Reuse core digest verification and schema upcasting without opening a writer."""
    with journal(root, sid) as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE study_id=? AND seq>? ORDER BY seq", (sid, after)
        ).fetchall()
    # model_dump(mode="json") is the EventWireDict wire shape (TypedDict, not a second schema).
    return [
        event_to_wire(stored_event_from_row(row)).model_dump(mode="json")  # type: ignore[misc]
        for row in rows
    ]
