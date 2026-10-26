"""Slow e2e: the recovery DRIVER kills and restores (plan gap 3, PR #221).

The unit suite (test_bench_drivers.py) proves the mappings; the CLI-lane kill/
restore loop is proven by tests/cli/test_continue_e2e_slow.py. This test proves
the DRIVER path end to end: ``LiveRunner`` branches on ``BenchTask.driver ==
"recovery"``, starts ``coscientist run``, polls the journal for the first EXP
``ResultRecorded`` (the configured kill marker), SIGKILLs the process group
mid-EXP-1 (the fake LLM deterministically holds that call), runs
``coscientist continue``, and returns an observed ``RunnerSuccess`` whose
quality the harness compares exactly against the task data.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from bench.live_runner import LiveRunner, LiveRunnerConfig
from bench.models import (
    EvidenceOutcomeCount,
    ExpectedMetrics,
    QualityExpectation,
    RunnerRefuse,
)
from bench.suite import run_suite

pytestmark = [pytest.mark.slow]

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "bench" / "lab" / "driver"
GLM_CATALOG = ROOT / "bench" / "lab" / "glm" / "checks_catalog.json"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fake_llm = _load("fake_llm_recovery_e2e", DRIVER / "fake_llm.py")
recovery_check = _load("recovery_check_e2e", DRIVER / "recovery_check.py")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


@pytest.fixture
def fake_llm_server(tmp_path: Path):
    """Fake OpenAI-compatible server; EXP-1 held 5s per call (kill window)."""
    run_dir = tmp_path / "run"
    port = _free_port()
    assert fake_llm.main(["fixtures", "--dir", str(run_dir), "--port", str(port)]) == 0
    bodies_dir = run_dir / "bodies"
    server = ThreadingHTTPServer(("127.0.0.1", port), fake_llm._Handler)
    server.cfg = {  # type: ignore[attr-defined]
        "bodies": {
            n: (bodies_dir / f"{n}.json").read_text(encoding="utf-8") for n in ("spec", "plan", "exec0", "exec1")
        },
        "killed": str(run_dir / "killed"),
        # Hold each EXP-1 LLM call 1s: the driver's marker poll (0.2s cadence)
        # kills inside the window, and continue's own EXP-1 call (also held 1s)
        # stays inside the 2s lease TTL (no epoch-fencing rejection).
        "exp1_hold_s": 1.0,
        "non_executor_seen": 0,
        "nz_lock": threading.Lock(),
        "requests_log": str(run_dir / "requests.log"),
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield run_dir
    server.shutdown()
    thread.join(timeout=5)


def _driver_config(fake_llm_server: Path, tmp_path: Path) -> LiveRunnerConfig:
    cfg: dict[str, Any] = {
        "llm_config": str(fake_llm_server / "llm.json"),
        "catalog": str(GLM_CATALOG),
        "catalog_version": 4,
        "profile": "research-glm",
        "tool_catalog_version": 2,
        "planner_model": "glm-4.7",
        "executor_model": "glm-4.7",
        "tz_spec_model": "glm-4.7",
        "owner": "bench-recovery-e2e",
        "tools_json": str(fake_llm_server / "tools.json"),
        "mcp_tool_bundle": None,
        "mcp_tool_call_args": None,
        "coder_backend": None,
        "coder_jobs": None,
        "task_inputs_dir": str(tmp_path / "suite"),
        "data_root": str(fake_llm_server / "data"),
        "study_id_prefix": "bench-recovery",
        "deadline_s": 600.0,
        "require_hypotheses": False,
        "confirmations_needed": 1,
        "refutations_needed": 1,
        "refutation_priority": False,
        "supported_needed": 2,
        "max_expansions": 0,
        "finalize_reserve_s": 30,
        "lease_ttl_s": 2.0,
        "rng_seed": 0,
        "max_llm_calls": 40,
        "max_tokens": 1000000,
        "max_tool_calls": 20,
        "max_ticks": 20,
        "hitl_mode": "headless",
        "wired_roles": [],
        "subprocess_cwd": str(ROOT),
        "timeout_s": 240.0,
        "driver_out_dir": str(tmp_path / "driver_out"),
        "web_host": "127.0.0.1",
        "web_port": 8421,
        "web_stream_poll_interval_s": 0.1,
        "web_run_wait_s": 30.0,
        "web_post_timeout_s": 45.0,
        "web_stream_timeout_s": 120.0,
        "web_connect_retries": 20,
        "recovery_kill_event_type": "ResultRecorded",
        "recovery_kill_entity_contains": "/EXP-",
        "recovery_kill_wait_s": 120.0,
        "recovery_lease_wait_s": 60.0,
    }
    return LiveRunnerConfig.model_validate(cfg)


def _recovery_task() -> Any:
    from bench.models import BenchTask

    return BenchTask.model_validate(
        {
            "task_id": "recovery_probe",
            "task_set_version": "1",
            "prompt": "Две задачи: dataset_overview и chemical_space_clustering (fixture text).",
            "inputs": (),
            "driver": "recovery",
            "expected_metrics": ExpectedMetrics(dur_s=0.0, llm_calls=0, tokens=0),
            "quality": QualityExpectation(
                status="fulfilled",
                exp_tasks=2,
                evidence=(EvidenceOutcomeCount(outcome="SUCCESS", count=2),),
            ),
        }
    )


def test_recovery_driver_sigkill_continue_restores(
    fake_llm_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "dummy-key-for-fake-llm")
    config = _driver_config(fake_llm_server, tmp_path)
    task = _recovery_task()
    study_id = "bench-recovery-recovery_probe"

    suite = run_suite(
        LiveRunner(config),
        (task,),
        system_id="new",
        task_set_id="t",
        task_set_version="1",
    )
    entry = suite.runs[0]
    run = entry.run
    assert run.kind == "ok", getattr(run, "detail", "")
    # Observed quality of the RESTORED study (journal fold, verdict as status).
    assert run.quality.status == "fulfilled"
    assert run.quality.exp_tasks == 2
    assert run.quality.evidence == (EvidenceOutcomeCount(outcome="SUCCESS", count=2),)
    assert run.quality.record_result == 2
    # Usage rows from the continue summary (same _usage_row shape as run).
    # Only the resumed work is in it: SPEC + EXP-0 were driven by the killed
    # run (its summary died with the process — usage is never reconstructed).
    roles = [record.role for record in run.usage_recorded]
    assert roles == ["executor-route"]
    assert entry.quality is not None
    assert entry.quality.status_equal is True
    assert entry.quality.exp_tasks_equal is True
    assert entry.quality.evidence_equal is True
    assert entry.quality.equal is True

    # The deep restore invariants via the same checker the lab driver uses.
    db = fake_llm_server / "data" / study_id / "events.db"
    summary = recovery_check.check(db, study_id)
    assert summary["violations"] == []
    assert summary["verdict"] == "fulfilled"
    assert summary["task_keys"].count("EXP-0") == 1
    assert summary["task_keys"].count("EXP-1") == 1

    # Driver artifacts: run/continue logs + the driver summary.
    out_dir = Path(config.driver_out_dir)
    assert (out_dir / f"{study_id}_run.log").is_file()
    assert (out_dir / f"{study_id}_continue.log").is_file()
    driver_summary = json.loads((out_dir / f"{study_id}_summary.json").read_text(encoding="utf-8"))
    assert driver_summary["kill_marker"]["seen"] is True
    assert driver_summary["continue_exit"] == 0
    assert driver_summary["verdict"] == "fulfilled"


def test_recovery_driver_refuses_existing_journal(fake_llm_server: Path, tmp_path: Path) -> None:
    config = _driver_config(fake_llm_server, tmp_path)
    journal = fake_llm_server / "data" / "bench-recovery-recovery_probe"
    journal.mkdir(parents=True)
    result = LiveRunner(config).run(_recovery_task())
    assert isinstance(result, RunnerRefuse)
    assert result.reason_code == "study_journal_exists"
