#!/usr/bin/env python3
"""Journal checker for the recovery lane (kill → restore → complete).

Verifies, from the SQLite journal alone (read-only):
  1. unique task keys — no duplicate EXP tasks grown by the resume;
  2. no closed attempt was re-executed: one AttemptStarted per attempt id,
     no second ResultRecorded for the same attempt, and nothing journaled
     against an attempt after its AttemptClosed;
  3. exactly one StudyFinalized and its verdict is recorded (fulfilled);
  4. at least two EXP tasks, each with a SUCCESS TaskDone.

Exit 0 + summary JSON on success; exit 1 with the violated invariant otherwise.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path


def _rows(conn: sqlite3.Connection, study_id: str) -> Iterable[sqlite3.Row]:
    return conn.execute(
        "SELECT seq, type, entity_key, idem_key, payload FROM events "
        "WHERE study_id=? ORDER BY seq",
        (study_id,),
    )


def check(db: Path, study_id: str) -> dict[str, object]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = list(_rows(conn, study_id))
    finally:
        conn.close()

    violations: list[str] = []

    task_keys: list[str] = []
    attempt_started: dict[str, sqlite3.Row] = {}
    attempt_closed_seq: dict[str, int] = {}
    results_by_attempt: dict[str, int] = {}
    finalized: list[sqlite3.Row] = []
    task_done: dict[str, dict[str, object]] = {}

    for row in rows:
        payload = json.loads(row["payload"]) if row["payload"] else {}
        etype, seq = row["type"], int(row["seq"])
        if etype == "TaskCreated":
            key = payload.get("task_key") or (payload.get("spec") or {}).get("task_key")
            if not key:
                violations.append(f"seq={seq}: TaskCreated without task_key")
            else:
                task_keys.append(str(key))
        elif etype == "AttemptStarted":
            aid = payload.get("attempt_id") or row["entity_key"]
            if aid in attempt_started:
                violations.append(f"attempt {aid}: second AttemptStarted")
            attempt_started[str(aid)] = row
        elif etype == "AttemptClosed":
            aid = payload.get("attempt_id") or row["entity_key"]
            attempt_closed_seq[str(aid)] = seq
        elif etype == "ResultRecorded":
            aid = str(payload.get("attempt_id") or row["entity_key"])
            results_by_attempt[aid] = results_by_attempt.get(aid, 0) + 1
        elif etype == "TaskDone":
            task_done[str(payload.get("task_id") or row["entity_key"])] = {
                "outcome": payload.get("outcome"),
                "reason": payload.get("reason"),
            }
        elif etype == "StudyFinalized":
            finalized.append(row)

        closed_at = attempt_closed_seq.get(
            str(payload.get("attempt_id") or row["entity_key"])
        )
        if (
            closed_at is not None
            and etype in ("ResultRecorded", "RouteReturned", "ToolCalled")
            and seq > closed_at
        ):
            violations.append(
                f"attempt {payload.get('attempt_id') or row['entity_key']}: "
                f"{etype} at seq={seq} after AttemptClosed at seq={closed_at}"
            )

    dupes = sorted({k for k in task_keys if task_keys.count(k) > 1})
    if dupes:
        violations.append(f"duplicate task keys: {dupes}")
    for aid, n in results_by_attempt.items():
        if n > 1:
            violations.append(f"attempt {aid}: {n} ResultRecorded events")
    if len(finalized) != 1:
        violations.append(f"expected exactly 1 StudyFinalized, found {len(finalized)}")

    verdict = None
    if finalized:
        verdict = json.loads(finalized[0]["payload"]).get("verdict")
    exp_keys = sorted(k for k in set(task_keys) if k.startswith("EXP-"))
    if len(exp_keys) < 2:
        violations.append(f"expected >=2 EXP tasks, found {exp_keys}")
    for key in exp_keys:
        # task_id is "<study>/<task_key>"; match by suffix.
        dones = [
            v for tid, v in task_done.items() if tid == key or tid.endswith(f"/{key}")
        ]
        outcome = str(dones[0]["outcome"]).lower() if len(dones) == 1 else None
        if len(dones) != 1 or outcome != "success":
            violations.append(f"{key}: expected exactly one success TaskDone, got {dones}")

    summary = {
        "study_id": study_id,
        "events": len(rows),
        "task_keys": sorted(set(task_keys)),
        "attempts": len(attempt_started),
        "results": sum(results_by_attempt.values()),
        "verdict": verdict,
        "violations": violations,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recovery_check")
    parser.add_argument("db", type=Path)
    parser.add_argument("study_id")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(f"no journal at {args.db}", file=sys.stderr)
        return 1
    summary = check(args.db, args.study_id)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if summary["violations"] or summary["verdict"] != "fulfilled":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
