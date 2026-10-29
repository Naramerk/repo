"""Slow e2e: real SIGKILL between processes → ``coscientist continue`` restores.

The unit suite (test_continue_cmd.py) proves replay/continue semantics with
fakes; this test proves the actual kill/restore loop the legacy ``recovery`` /
``recovery2`` benchmarks exercised: run in its own process group, poll the
journal for the first EXP ``ResultRecorded`` *and* an open EXP-1
``AttemptStarted`` (so SIGKILL lands mid-EXP-1 while the fake LLM holds that
call), then continue on the same journal and verify via the same checker the
lab driver uses (bench/lab/driver/recovery_check.py).
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "bench" / "lab" / "driver"
CATALOG = ROOT / "bench" / "lab" / "glm" / "checks_catalog.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fake_llm = _load("fake_llm_e2e", DRIVER / "fake_llm.py")
recovery_check = _load("recovery_check_e2e", DRIVER / "recovery_check.py")


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _exp_results(db: Path) -> int:
    if not db.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return int(
                conn.execute(
                    "SELECT count(*) FROM events WHERE type='ResultRecorded' AND entity_key LIKE '%/EXP-%'"
                ).fetchone()[0]
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _open_exp1_attempt(db: Path) -> bool:
    """True when EXP-1 has AttemptStarted and that attempt is still open."""
    if not db.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            started = conn.execute("SELECT entity_key, payload FROM events WHERE type='AttemptStarted'").fetchall()
            closed = {
                row[0] for row in conn.execute("SELECT entity_key FROM events WHERE type='AttemptClosed'").fetchall()
            }
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    for entity_key, payload_raw in started:
        payload = json.loads(payload_raw) if payload_raw else {}
        task_id = str(payload.get("task_id") or "")
        if task_id == "EXP-1" or task_id.endswith("/EXP-1"):
            aid = str(payload.get("attempt_id") or entity_key)
            if aid not in closed and entity_key not in closed:
                return True
    return False


@pytest.mark.slow
def test_continue_after_sigkill_restores_and_completes(tmp_path: Path) -> None:
    study_id = "e2e-sigkill-continue"
    port = _free_port()
    run_dir = tmp_path / "run"
    assert fake_llm.main(["fixtures", "--dir", str(run_dir), "--port", str(port)]) == 0

    server = ThreadingHTTPServer(("127.0.0.1", port), fake_llm._Handler)
    bodies_dir = run_dir / "bodies"
    server.cfg = {  # type: ignore[attr-defined]
        "bodies": {
            n: (bodies_dir / f"{n}.json").read_text(encoding="utf-8") for n in ("spec", "plan", "exec0", "exec1")
        },
        "killed": str(run_dir / "killed"),
        "exp1_hold_s": 120.0,
        "non_executor_seen": 0,
        "nz_lock": threading.Lock(),
        "requests_log": str(run_dir / "requests.log"),
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    common = [
        "--catalog",
        str(CATALOG),
        "--catalog-version",
        "4",
        "--profile",
        "research-glm",
        "--tool-catalog-version",
        "2",
        "--owner",
        "e2e-recovery",
        "--llm-config",
        str(run_dir / "llm.json"),
        "--planner-model",
        "glm-4.7",
        "--executor-model",
        "glm-4.7",
        "--tz-spec-model",
        "glm-4.7",
        "--tools-json",
        str(run_dir / "tools.json"),
        "--study-id",
        study_id,
        "--rng-seed",
        "0",
        "--max-llm-calls",
        "40",
        "--max-tokens",
        "1000000",
        "--max-tool-calls",
        "20",
        "--max-ticks",
        "20",
    ]
    env = {**os.environ, "COSCI_TEST_LLM_KEY": "dummy-key-for-fake-llm"}
    db = run_dir / "data" / study_id / "events.db"

    # Phase 1: run until EXP-0 is recorded *and* EXP-1 attempt is open (held by
    # fake LLM), then SIGKILL so continue must close that crashed attempt.
    run_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "coscientist.cli",
            "run",
            str(run_dir / "task.txt"),
            "--data-root",
            str(run_dir / "data"),
            "--lease-ttl-s",
            "2",
            "--require-hypotheses",
            "false",
            "--confirmations-needed",
            "1",
            "--refutations-needed",
            "1",
            "--refutation-priority",
            "false",
            "--supported-needed",
            "2",
            "--max-expansions",
            "0",
            "--hitl-mode",
            "headless",
            "--deadline-s",
            "600",
            "--finalize-reserve-s",
            "30",
            *common,
        ],
        cwd=str(ROOT),
        env=env,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 120
        while time.time() < deadline and not (_exp_results(db) >= 1 and _open_exp1_attempt(db)):
            time.sleep(0.1)
        assert _exp_results(db) >= 1, "run never recorded the first EXP result"
        assert _open_exp1_attempt(db), "EXP-1 attempt never opened before kill window"
        (run_dir / "killed").write_text("killed\n", encoding="utf-8")
        os.killpg(os.getpgid(run_proc.pid), signal.SIGKILL)
        run_proc.wait(timeout=30)
        assert run_proc.returncode == -signal.SIGKILL
    finally:
        if run_proc.poll() is None:
            os.killpg(os.getpgid(run_proc.pid), signal.SIGKILL)
            run_proc.wait(timeout=30)

    # The kill landed mid-study: EXP-0 done, no finalize yet.
    summary_before = recovery_check.check(db, study_id)
    assert "EXP-0" in summary_before["task_keys"]
    assert summary_before["verdict"] is None

    # Phase 2: continue on the same journal (waits out the 2s lease).
    cont = subprocess.run(
        [
            sys.executable,
            "-m",
            "coscientist.cli",
            "continue",
            str(run_dir / "data"),
            "--lease-ttl-s",
            "2",
            "--lease-wait-s",
            "60",
            *common,
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert cont.returncode == 0, cont.stdout + cont.stderr
    line = cont.stdout.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["phase"] == "closed"
    assert payload["verdict"] == "fulfilled"
    assert payload["recovery"]["closed"] >= 1, "the crashed EXP-1 attempt was closed"

    summary = recovery_check.check(db, study_id)
    assert summary["violations"] == []
    assert summary["verdict"] == "fulfilled"
    assert summary["task_keys"].count("EXP-0") == 1
    assert summary["task_keys"].count("EXP-1") == 1
    assert summary["results"] == 3  # SPEC + EXP-0 + EXP-1 (exactly one per attempt)

    server.shutdown()
    thread.join(timeout=5)
