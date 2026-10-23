#!/usr/bin/env python3
"""Build ``tests/fixtures/journals/reclassified_fetch_old/events.db`` from JSON.

Source of truth: ``tests/fixtures/journals/reclassified_fetch_old.json``.
Re-run after editing the JSON::

    python scripts/gen_reclassified_fetch_old_journal.py

The DB is written with ``JOURNAL_STORE_FORMAT`` so ``SqliteEventStore`` will open it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "tests/fixtures/journals/reclassified_fetch_old.json"
OUT_DIR = ROOT / "tests/fixtures/journals/reclassified_fetch_old"
OUT_DB = OUT_DIR / "events.db"


def main() -> int:
    # Import after path is stable so editable installs resolve.
    from coscientist.core.events.digest import payload_digest
    from coscientist.core.events.store_db import (
        JOURNAL_STORE_FORMAT,
        ensure_journal_store_format,
        init_schema,
    )

    meta = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    study_id = str(meta["study_id"])
    events = meta["events"]
    if not isinstance(events, list) or not events:
        raise SystemExit(f"{JSON_PATH}: missing events[]")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUT_DB.exists():
        OUT_DB.unlink()
    # WAL sidecars from prior runs
    for side in (OUT_DB.with_suffix(".db-wal"), OUT_DB.with_suffix(".db-shm")):
        if side.exists():
            side.unlink()

    conn = sqlite3.connect(str(OUT_DB))
    try:
        init_schema(conn)
        ensure_journal_store_format(conn)
        for seq, ev in enumerate(events, start=1):
            payload = ev["payload"]
            digest = payload_digest(payload)
            conn.execute(
                "INSERT INTO events("
                "seq, study_id, type, entity_key, idem_key, payload, ts, "
                "schema_version, epoch, payload_digest"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    study_id,
                    ev["type"],
                    ev["entity_key"],
                    ev["idem_key"],
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    ev["ts"],
                    int(ev["schema_version"]),
                    int(ev.get("epoch", 0)),
                    digest,
                ),
            )
        conn.commit()
        fmt = conn.execute(
            "SELECT value FROM journal_meta WHERE key='store_format'"
        ).fetchone()
        assert fmt is not None and int(fmt[0]) == JOURNAL_STORE_FORMAT
        n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()

    print(f"wrote {OUT_DB} ({n} events, store_format={JOURNAL_STORE_FORMAT})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
