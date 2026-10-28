#!/usr/bin/env python3
"""Watch API-launched studies; auto-accept plan_approval gates; report terminals.

Usage: python3 watch_and_approve.py <study_id> [<study_id> ...]
Polls 127.0.0.1:8487 every 15s. Exits 0 when every study is terminal.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8487"
POLL_S = 15.0


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return json.load(r)


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)  # type: ignore[no-any-return]
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:300]}


def light_status(sid: str) -> dict:
    # /console/runs/{sid} reads the launch mapping (no runtime lease needed);
    # GET /studies/{sid} opens the runtime and 500s with LeaseLost while running.
    return _get(f"/console/runs/{sid}")


def terminal(status: dict) -> str | None:
    phase = status.get("phase")
    if phase == "closed":
        return f"closed verdict={status.get('verdict')}"
    rs = status.get("run_status")
    if rs in ("refused", "failed", "cancelled", "completed"):
        return f"run_status={rs} {status.get('detail') or ''}"
    return None


def main() -> int:
    studies = sys.argv[1:]
    if not studies:
        print("usage: watch_and_approve.py <study_id>...", file=sys.stderr)
        return 2
    done: dict[str, str] = {}
    approved: set[str] = set()
    while len(done) < len(studies):
        for sid in studies:
            if sid in done:
                continue
            try:
                status = light_status(sid)
                term = terminal(status)
                if term:
                    full = _get(f"/studies/{sid}")
                    done[sid] = f"{term} verdict={full.get('verdict')}"
                    print(f"TERMINAL[{sid}]: {done[sid]}", flush=True)
                    continue
            except Exception as e:  # noqa: BLE001 — watcher must survive restarts
                print(f"POLL-ERR {sid}: {e}", flush=True)
                continue
            gates: dict | list = {}
            try:
                gates = _get(f"/console/studies/{sid}/hitl")
            except Exception:  # noqa: BLE001
                pass
            gate_rows: list = gates if isinstance(gates, list) else []
            for g in gate_rows:
                if not g.get("open") or g.get("kind") != "plan_approval":
                    continue
                rid = g["request_id"]
                if rid in approved:
                    continue
                prompt = g.get("prompt") or {}
                sha = prompt.get("context_sha256")
                seq = g.get("opened_seq")
                if not sha or not seq:
                    print(f"SKIP {sid} {rid}: missing sha/seq", flush=True)
                    continue
                res = _post(f"/studies/{sid}/hitl/{rid}", {
                    "decision": "accept",
                    "opened_seq": seq,
                    "context_sha256": sha,
                })
                approved.add(rid)
                ok = "http_error" not in res
                print(f"APPROVE[{sid}] {rid} seq={seq}: {'ok' if ok else res}", flush=True)
        time.sleep(POLL_S)
    print("ALL TERMINAL", flush=True)
    for sid in studies:
        print(f"FINAL {sid}: {done[sid]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
