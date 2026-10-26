"""Slow e2e: the web DRIVER drives the uvicorn API lane (plan gap 3, PR #220).

Proves the DRIVER path end to end: ``LiveRunner`` branches on
``BenchTask.driver == "web"``, starts ``python -m coscientist.api.serve`` as a
subprocess (all flags explicit), POSTs the study run, collects the WS journal
stream to JSONL, receives ``StudyFinalized``, and maps the served run summary
through the SHARED CLI-lane mapping. Also pins the drivers' journal observer to
the authoritative run-summary fold: both must agree on the same closed study.

Requires the launcher-supplied runtime deps (uvicorn, websockets) — skipped
where they are absent (plain CI env; run with the api extras + the two deps
for the live check).
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

pytest.importorskip("uvicorn")
pytest.importorskip("websockets")

from bench.drivers.journal import observe_quality  # noqa: E402
from bench.live_runner import LiveRunner, LiveRunnerConfig  # noqa: E402
from bench.models import (  # noqa: E402
    EvidenceOutcomeCount,
    ExpectedMetrics,
    QualityExpectation,
)
from bench.suite import run_suite  # noqa: E402
from coscientist.core.events.ids import study_id_from_idempotency_key  # noqa: E402

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


fake_llm = _load("fake_llm_web_e2e", DRIVER / "fake_llm.py")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


@pytest.fixture
def fake_llm_server(tmp_path: Path):
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
        "exp1_hold_s": 0.0,
        "non_executor_seen": 0,
        "nz_lock": threading.Lock(),
        "requests_log": str(run_dir / "requests.log"),
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield run_dir
    server.shutdown()
    thread.join(timeout=5)


def _driver_config(fake_llm_server: Path, tmp_path: Path, *, port: int) -> LiveRunnerConfig:
    cfg: dict[str, Any] = {
        "llm_config": str(fake_llm_server / "llm.json"),
        "catalog": str(GLM_CATALOG),
        "catalog_version": 4,
        "profile": "research-glm",
        "tool_catalog_version": 2,
        "planner_model": "glm-4.7",
        "executor_model": "glm-4.7",
        "tz_spec_model": "glm-4.7",
        "owner": "bench-web-e2e",
        "tools_json": str(fake_llm_server / "tools.json"),
        "mcp_tool_bundle": None,
        "mcp_tool_call_args": None,
        "coder_backend": None,
        "coder_jobs": None,
        "task_inputs_dir": str(tmp_path / "suite"),
        "data_root": str(fake_llm_server / "data"),
        "study_id_prefix": "bench-web",
        "deadline_s": 600.0,
        "require_hypotheses": False,
        "confirmations_needed": 1,
        "refutations_needed": 1,
        "refutation_priority": False,
        "supported_needed": 2,
        "max_expansions": 0,
        "finalize_reserve_s": 30,
        "lease_ttl_s": 60.0,
        "rng_seed": 0,
        "max_llm_calls": 40,
        "max_tokens": 1000000,
        "max_tool_calls": 20,
        "max_ticks": 20,
        "hitl_mode": "headless",
        "wired_roles": [],
        "subprocess_cwd": str(ROOT),
        "timeout_s": 300.0,
        "driver_out_dir": str(tmp_path / "driver_out"),
        "web_host": "127.0.0.1",
        "web_port": port,
        "web_stream_poll_interval_s": 0.05,
        "web_run_wait_s": 30.0,
        "web_post_timeout_s": 45.0,
        "web_stream_timeout_s": 180.0,
        "web_connect_retries": 240,
        "recovery_kill_event_type": "ResultRecorded",
        "recovery_kill_entity_contains": "/EXP-",
        "recovery_kill_wait_s": 120.0,
        "recovery_lease_wait_s": 60.0,
    }
    return LiveRunnerConfig.model_validate(cfg)


def _web_task() -> Any:
    from bench.models import BenchTask

    return BenchTask.model_validate(
        {
            "task_id": "web_probe",
            "task_set_version": "1",
            "prompt": "Две задачи: dataset_overview и chemical_space_clustering (fixture text).",
            "inputs": (),
            "driver": "web",
            "expected_metrics": ExpectedMetrics(dur_s=0.0, llm_calls=4, tokens=32),
            "quality": QualityExpectation(
                status="fulfilled",
                exp_tasks=2,
                evidence=(EvidenceOutcomeCount(outcome="SUCCESS", count=2),),
            ),
        }
    )


def test_web_driver_api_subprocess_restores_full_lane(
    fake_llm_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "dummy-key-for-fake-llm")
    port = _free_port()
    config = _driver_config(fake_llm_server, tmp_path, port=port)
    task = _web_task()
    # The API lane enforces the core study-id format: the driver maps the bench
    # study id onto the deterministic idempotent id (core helper, same source).
    study_id = study_id_from_idempotency_key("bench-web-web_probe")

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
    # Observed quality via the SERVED run summary (the shared CLI-lane mapping).
    assert run.quality.status == "fulfilled"
    assert run.quality.exp_tasks == 2
    assert run.quality.evidence == (EvidenceOutcomeCount(outcome="SUCCESS", count=2),)
    assert run.quality.record_result == 2
    roles = [record.role for record in run.usage_recorded]
    assert roles[0] == "tz_spec"
    assert "executor-route" in roles
    assert entry.quality is not None and entry.quality.equal is True

    # Lane artifacts: WS events JSONL + the webdrive-shaped summary.
    out_dir = Path(config.driver_out_dir)
    events_lines = (out_dir / f"{study_id}_events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in events_lines]
    assert any(event.get("type") == "StudyFinalized" for event in events)
    driver_summary = json.loads((out_dir / f"{study_id}_summary.json").read_text(encoding="utf-8"))
    assert driver_summary["final"]["run_status"] == "done"
    assert driver_summary["final"]["verdict"] == "fulfilled"

    # Parity pin: the drivers' journal observer agrees with the authoritative
    # run-summary fold on the same closed study (no second fold drift).
    observed = observe_quality(events)
    assert observed["exp_tasks"] == run.quality.exp_tasks
    assert observed["evidence"] == [
        {"outcome": row.outcome, "count": row.count} for row in run.quality.evidence
    ]
    assert observed["record_result"] == run.quality.record_result
    assert observed["verdict"] == run.quality.status
