"""Read-only journal observation for the driver lanes (web / recovery).

One narrow observer shared by both drivers — NOT a second validation path:
fail-closed validation stays in the CLI / API lane single source. The drivers
see only wire events (the WS stream frames, or read-only ``events.db`` rows),
so this module folds exactly the fields the drivers must OBSERVE to report an
honest outcome when the lane's run-summary is not served (a run that died
mid-way, or a closure the API lane refuses to serve a summary for):

- ``exp_tasks``: count of EXPERIMENT-kind tasks (``kind == "experiment"``
  TaskCreated / TaskInherited dumps, by unique ``task_id`` — same semantics as
  the run summary's ``_observed_quality`` fold; kind comes from the task dump,
  never id parsing);
- ``evidence``: outcome tallies over the experiments' recorded results,
  deduplicated by payload ``result_id`` (the runtime fold double-keys rows by
  ``result_id`` + ``attempt_id``), sorted by outcome name — the same
  deterministic shape as the summary ``quality`` block;
- ``record_result``: count of those deduplicated experiment result rows;
- ``verdict``: the payload verdict of the LAST ``StudyFinalized`` event
  (``None`` while the study is open).

A parity test (tests/test_bench_driver_web_e2e.py) pins this observer to the
authoritative run-summary fold on a real closed study: both must agree.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from bench.errors import BenchConfigError

# Journal event that carries the terminal verdict.
_EXPERIMENT_KIND = "experiment"


def observe_quality(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Observed quality from wire-event mappings (``type`` + ``payload`` keys).

    Mirrors the run-summary ``_observed_quality`` fold over journal state:
    experiment tasks by kind, result rows deduped by ``result_id``, tallies
    sorted by outcome. ``verdict`` is the last ``StudyFinalized`` payload
    verdict (``None`` while the study is open).
    """
    kind_by_task: dict[str, str] = {}
    results: dict[str, dict[str, Any]] = {}
    verdict: str | None = None
    finalized = 0
    n_events = 0
    for event in events:
        n_events += 1
        etype = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if etype in ("TaskCreated", "TaskInherited"):
            task_id = payload.get("task_id")
            kind = payload.get("kind")
            if isinstance(task_id, str) and isinstance(kind, str):
                kind_by_task[task_id] = kind
        elif etype == "ResultRecorded":
            result_id = payload.get("result_id")
            if isinstance(result_id, str) and result_id:
                results[result_id] = dict(payload)
        elif etype == "StudyFinalized":
            finalized += 1
            raw_verdict = payload.get("verdict")
            if isinstance(raw_verdict, str):
                verdict = raw_verdict
    exp_results = [
        row for row in results.values() if kind_by_task.get(str(row.get("task_id"))) == _EXPERIMENT_KIND
    ]
    tally: Counter[str] = Counter(str(row["outcome"]) for row in exp_results if "outcome" in row)
    return {
        "exp_tasks": sum(1 for kind in kind_by_task.values() if kind == _EXPERIMENT_KIND),
        "evidence": [{"outcome": outcome, "count": tally[outcome]} for outcome in sorted(tally)],
        "record_result": len(exp_results),
        "verdict": verdict,
        "finalized": finalized,
        "n_events": n_events,
    }


def _events_table_ready(conn: sqlite3.Connection) -> bool:
    """True once the journal schema (``events`` table) is committed.

    The writer's ``sqlite3.connect`` creates the db file BEFORE the schema
    commit (``SqliteEventStore.__init__``: connect → pragmas → init_schema),
    so a visible file is not yet a pollable journal. In that window the file
    is a valid empty database — "no events yet" per the pre-db contract, never
    ``sqlite3.OperationalError: no such table: events`` in the poll loops.
    """
    query = "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='events'"
    row = conn.execute(query).fetchone()
    return int(row[0]) > 0


def read_journal_events(db: Path, study_id: str) -> list[dict[str, Any]]:
    """Read-only journal rows as wire-shaped dicts (``[]`` before the db exists).

    A journal that has not been created yet has no events — the drivers poll
    through here, so a missing file is "no events yet", never an error. The
    same applies to the file-present-but-schema-absent creation window.
    """
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BenchConfigError(f"read-only journal open failed: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        if not _events_table_ready(conn):
            return []
        return list(_iter_rows(conn, study_id))
    finally:
        conn.close()


def _iter_rows(conn: sqlite3.Connection, study_id: str) -> Iterator[dict[str, Any]]:
    cursor = conn.execute(
        "SELECT seq, type, entity_key, payload FROM events WHERE study_id=? ORDER BY seq",
        (study_id,),
    )
    for row in cursor:
        payload = json.loads(row["payload"]) if row["payload"] else {}
        yield {
            "seq": int(row["seq"]),
            "type": row["type"],
            "entity_key": row["entity_key"],
            "payload": payload,
        }


def count_marker_events(db: Path, study_id: str, *, event_type: str, entity_contains: str) -> int:
    """Count journal events matching the kill marker (read-only; ``0`` pre-db).

    The marker is the driver's configured kill-after observation:
    ``type == event_type`` and ``entity_contains in entity_key``. A journal
    whose schema has not committed yet is still pre-db (``0``), matching the
    poll contract — the driver's 0.2s poll races the writer's schema commit.
    """
    if not db.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BenchConfigError(f"read-only journal open failed: {exc}") from exc
    try:
        if not _events_table_ready(conn):
            return 0
        row = conn.execute(
            "SELECT count(*) FROM events WHERE study_id=? AND type=? AND instr(entity_key, ?) > 0",
            (study_id, event_type, entity_contains),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


__all__ = [
    "count_marker_events",
    "observe_quality",
    "read_journal_events",
]
