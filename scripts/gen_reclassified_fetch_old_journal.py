#!/usr/bin/env python3
"""Build ``tests/fixtures/journals/reclassified_fetch_old/events.db`` from JSON.

Source of truth: ``tests/fixtures/journals/reclassified_fetch_old.json``.
Re-run after editing the JSON (repo root on ``PYTHONPATH``)::

    PYTHONPATH=. python scripts/gen_reclassified_fetch_old_journal.py

Verify the committed DB matches the generator::

    PYTHONPATH=. python scripts/gen_reclassified_fetch_old_journal.py --check

The DB is written with ``JOURNAL_STORE_FORMAT`` so ``SqliteEventStore`` will open it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "tests/fixtures/journals/reclassified_fetch_old.json"
OUT_DIR = ROOT / "tests/fixtures/journals/reclassified_fetch_old"
OUT_DB = OUT_DIR / "events.db"


def _require_exact_int(value: object, *, what: str) -> int:
    if type(value) is not int:
        raise SystemExit(
            f"{what} must be exact int, got {type(value).__name__}: {value!r}"
        )
    return value


def _logical_fingerprint(db_path: Path) -> list[Any]:
    conn = sqlite3.connect(str(db_path))
    try:
        meta = list(
            conn.execute("SELECT key, value FROM journal_meta ORDER BY key")
        )
        events = list(
            conn.execute(
                "SELECT seq, study_id, type, entity_key, idem_key, payload, ts, "
                "schema_version, epoch, payload_digest FROM events ORDER BY seq"
            )
        )
    finally:
        conn.close()
    return [meta, events]


def build_db(out_db: Path) -> int:
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

    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()
    for side in (out_db.with_suffix(".db-wal"), out_db.with_suffix(".db-shm")):
        if side.exists():
            side.unlink()

    conn = sqlite3.connect(str(out_db))
    try:
        init_schema(conn)
        ensure_journal_store_format(conn)
        for seq, ev in enumerate(events, start=1):
            payload = ev["payload"]
            digest = payload_digest(payload)
            schema_version = _require_exact_int(
                ev["schema_version"], what=f"events[{seq - 1}].schema_version"
            )
            if "epoch" in ev:
                epoch = _require_exact_int(ev["epoch"], what=f"events[{seq - 1}].epoch")
            else:
                epoch = 0
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
                    schema_version,
                    epoch,
                    digest,
                ),
            )
        conn.commit()
        fmt = conn.execute(
            "SELECT value FROM journal_meta WHERE key='store_format'"
        ).fetchone()
        assert fmt is not None and fmt[0] == str(JOURNAL_STORE_FORMAT)
        n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    return int(n)


def check_committed() -> int:
    if not OUT_DB.is_file():
        raise SystemExit(f"missing committed DB: {OUT_DB}")
    with tempfile.TemporaryDirectory() as td:
        generated = Path(td) / "events.db"
        build_db(generated)
        if _logical_fingerprint(OUT_DB) != _logical_fingerprint(generated):
            raise SystemExit(
                f"{OUT_DB} does not match generator output from {JSON_PATH}; "
                f"re-run without --check to rebuild"
            )
    print(f"OK: {OUT_DB} matches generator")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify committed events.db matches JSON generator (no write)",
    )
    args = parser.parse_args(argv)
    if args.check:
        return check_committed()
    n = build_db(OUT_DB)
    from coscientist.core.events.store_db import JOURNAL_STORE_FORMAT

    print(f"wrote {OUT_DB} ({n} events, store_format={JOURNAL_STORE_FORMAT})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
