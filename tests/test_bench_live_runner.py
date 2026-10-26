"""Tests for the live new-system runner (plan item 13, bench/live_runner.py).

Covers the typed refusal paths, the run-summary parse (canned lines), the
config surface (every field required, cassette XOR live MCP, driver bounds),
and one e2e that drives the real CLI subprocess over the local fake-LLM HTTP
server (bench/lab/driver/fake_llm.py) asserting the harness quality compare
passes. Driver-lane tests live in test_bench_drivers.py (unit) and the
test_bench_driver_*_e2e.py files.
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
from pydantic import ValidationError

from bench.clilane import result_from_process
from bench.live_runner import LiveRunner, LiveRunnerConfig
from bench.models import (
    BenchTask,
    EvidenceOutcomeCount,
    ExpectedMetrics,
    QualityExpectation,
    RunnerRefuse,
    RunnerSuccess,
)
from bench.runsummary import parse_summary, stderr_tail
from bench.suite import run_suite

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "bench" / "lab" / "driver"
GLM_CATALOG = ROOT / "bench" / "lab" / "glm" / "checks_catalog.json"


def _load_fake_llm() -> Any:
    spec = importlib.util.spec_from_file_location("fake_llm_bench", DRIVER / "fake_llm.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fake_llm: Any = _load_fake_llm()


def _task(**overrides: Any) -> BenchTask:
    base: dict[str, Any] = {
        "task_id": "config_probe",
        "task_set_version": "1",
        "prompt": "Fixture task text (content is irrelevant to the refusal paths).",
        "inputs": (),
        "driver": None,
        "expected_metrics": ExpectedMetrics(dur_s=1.0, llm_calls=1, tokens=8),
        "quality": QualityExpectation(
            status="fulfilled",
            exp_tasks=1,
            evidence=(EvidenceOutcomeCount(outcome="SUCCESS", count=1),),
        ),
    }
    base.update(overrides)
    return BenchTask.model_validate(base)


def _config_dict(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """A complete explicit LiveRunnerConfig field set (paths need not exist for
    the refusal-path tests; the e2e overwrites the real fixture paths)."""
    cfg: dict[str, Any] = {
        "llm_config": str(tmp_path / "llm.json"),
        "catalog": str(tmp_path / "catalog.json"),
        "catalog_version": 4,
        "profile": "research-glm",
        "tool_catalog_version": 2,
        "planner_model": "glm-4.7",
        "executor_model": "glm-4.7",
        "tz_spec_model": "glm-4.7",
        "owner": "bench-test",
        "tools_json": str(tmp_path / "tools.json"),
        "mcp_tool_bundle": None,
        "mcp_tool_call_args": None,
        "coder_backend": None,
        "coder_jobs": None,
        "task_inputs_dir": str(tmp_path / "suite"),
        "data_root": str(tmp_path / "data"),
        "study_id_prefix": "bench-test",
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
        "timeout_s": 120.0,
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
        "recovery_lease_wait_s": 2.0,
    }
    cfg.update(overrides)
    return cfg


# --- config surface: every field required, tool-path XOR -----------------------


def test_config_requires_every_field(tmp_path: Path) -> None:
    cfg = _config_dict(tmp_path)
    del cfg["owner"]
    with pytest.raises(ValidationError) as excinfo:
        LiveRunnerConfig.model_validate(cfg)
    assert "owner" in str(excinfo.value)


def test_config_rejects_unknown_fields(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LiveRunnerConfig.model_validate(_config_dict(tmp_path, no_such_flag=True))


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"tools_json": None, "mcp_tool_bundle": None}, "one of tools_json or mcp_tool_bundle"),
        (
            {"tools_json": "/x/tools.json", "mcp_tool_bundle": "/x/bundle.json"},
            "mutually exclusive",
        ),
        (
            {
                "tools_json": None,
                "mcp_tool_bundle": "/x/bundle.json",
                "mcp_tool_call_args": None,
            },
            "mcp_tool_call_args is required with mcp_tool_bundle",
        ),
        (
            {"mcp_tool_call_args": "/x/args.json"},
            "mcp_tool_call_args is only valid with mcp_tool_bundle",
        ),
        ({"coder_jobs": "/x/jobs.json"}, "coder_backend is required with coder_jobs"),
    ],
)
def test_config_tool_path_and_coder_pairing_refuses(
    tmp_path: Path, overrides: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(ValidationError, match=fragment.replace("(", "\\(").replace(")", "\\)")):
        LiveRunnerConfig.model_validate(_config_dict(tmp_path, **overrides))


def test_config_from_yaml_file_round_trip(tmp_path: Path) -> None:
    yaml_path = tmp_path / "runner.yaml"
    cfg_dict = _config_dict(tmp_path, wired_roles=["critic"])
    yaml_path.write_text(
        "\n".join(f"{k}: {json.dumps(v)}" for k, v in cfg_dict.items()), encoding="utf-8"
    )
    config = LiveRunnerConfig.from_yaml_file(yaml_path)
    assert config.wired_roles == ("critic",)
    assert config.tools_json == Path(cfg_dict["tools_json"])
    assert config.hitl_mode == "headless"


def test_config_from_yaml_file_missing_refuses(tmp_path: Path) -> None:
    from bench.errors import BenchConfigError

    with pytest.raises(BenchConfigError, match="runner config missing"):
        LiveRunnerConfig.from_yaml_file(tmp_path / "missing.yaml")


def test_config_from_yaml_file_non_mapping_refuses(tmp_path: Path) -> None:
    from bench.errors import BenchConfigError

    yaml_path = tmp_path / "runner.yaml"
    yaml_path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(BenchConfigError, match="must be a YAML mapping"):
        LiveRunnerConfig.from_yaml_file(yaml_path)


def test_config_from_yaml_file_invalid_field_refuses(tmp_path: Path) -> None:
    from bench.errors import BenchConfigError

    yaml_path = tmp_path / "runner.yaml"
    cfg_dict = _config_dict(tmp_path)
    cfg_dict["hitl_mode"] = "sometimes"
    yaml_path.write_text(
        "\n".join(f"{k}: {json.dumps(v)}" for k, v in cfg_dict.items()), encoding="utf-8"
    )
    with pytest.raises(BenchConfigError, match="runner config invalid"):
        LiveRunnerConfig.from_yaml_file(yaml_path)


# --- runner refusal paths (typed, no subprocess for data problems) --------------


def test_run_driver_task_branches_to_driver_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Driver selection is task data, not config: each declared driver routes to
    # its lane AFTER the shared preflight (journal guard, inputs), with the
    # same study id and resolved inputs the CLI lane would get.
    import bench.live_runner as lr

    calls: list[dict[str, Any]] = []

    class _RecordingDriver:
        def run(self, task: Any, *, study_id: str, input_paths: Any, config: Any) -> RunnerRefuse:
            calls.append(
                {"driver": task.driver, "study_id": study_id, "input_paths": input_paths, "config": config}
            )
            return RunnerRefuse(kind="refused", reason_code="lane_probe", detail="branch probe")

    monkeypatch.setitem(lr._DRIVERS, "web", _RecordingDriver())
    Path(cfg_dir := tmp_path / "inputs").mkdir()
    (cfg_dir / "probe.csv").write_text("x\n", encoding="utf-8")
    cfg = _config_dict(tmp_path, task_inputs_dir=str(cfg_dir))
    runner = LiveRunner(LiveRunnerConfig.model_validate(cfg))
    result = runner.run(_task(task_id="driver_probe", driver="web", inputs=("probe.csv",)))
    assert isinstance(result, RunnerRefuse)
    assert result.reason_code == "lane_probe"
    assert calls[0]["study_id"] == "bench-test-driver_probe"
    assert calls[0]["input_paths"] == (cfg_dir / "probe.csv",)
    assert calls[0]["config"] is runner.config


def test_run_existing_journal_refuses(tmp_path: Path) -> None:
    cfg = _config_dict(tmp_path)
    journal = Path(cfg["data_root"]) / f"{cfg['study_id_prefix']}-config_probe"
    journal.mkdir(parents=True)
    runner = LiveRunner(LiveRunnerConfig.model_validate(cfg))
    result = runner.run(_task())
    assert isinstance(result, RunnerRefuse)
    assert result.reason_code == "study_journal_exists"


def test_run_missing_task_input_refuses(tmp_path: Path) -> None:
    cfg = _config_dict(tmp_path)
    Path(cfg["task_inputs_dir"]).mkdir()
    runner = LiveRunner(LiveRunnerConfig.model_validate(cfg))
    result = runner.run(_task(inputs=("data/config_probe/absent.csv",)))
    assert isinstance(result, RunnerRefuse)
    assert result.reason_code == "task_input_missing"


def test_run_subprocess_timeout_refuses(tmp_path: Path) -> None:
    # The CLI subprocess cannot boot in 0.05s (imports alone dominate), so the
    # wall-time cap deterministically lands while the child is starting.
    cfg = _config_dict(tmp_path, timeout_s=0.05)
    runner = LiveRunner(LiveRunnerConfig.model_validate(cfg))
    result = runner.run(_task())
    assert isinstance(result, RunnerRefuse)
    assert result.reason_code == "timeout"
    assert "exceeded timeout_s=0.05" in result.detail


def test_run_cli_config_refusal_maps_to_exit_refuse(tmp_path: Path) -> None:
    # A real subprocess refusal: broken --coder-jobs JSON exits 2 through the
    # CLI's own fail-closed validation; the adapter maps it to RunnerRefuse.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    bad_jobs = run_dir / "jobs.json"
    bad_jobs.write_text("{not json", encoding="utf-8")
    cfg = _config_dict(
        tmp_path,
        coder_backend="local-python",
        coder_jobs=str(bad_jobs),
        data_root=str(run_dir / "data"),
    )
    runner = LiveRunner(LiveRunnerConfig.model_validate(cfg))
    result = runner.run(_task())
    assert isinstance(result, RunnerRefuse)
    assert result.reason_code == "exit_2"
    assert "must be valid JSON" in result.detail


# --- summary parse (canned lines; the pure result mapping) ----------------------


def _summary_line(
    *,
    verdict: str | None = "fulfilled",
    exp_tasks: int = 2,
    evidence: list[dict[str, Any]] | None = None,
    record_result: int = 2,
) -> str:
    return json.dumps(
        {
            "study_id": "bench-e2e-live_probe",
            "phase": "closed",
            "verdict": verdict,
            "quality": {
                "exp_tasks": exp_tasks,
                "evidence": evidence if evidence is not None else [{"outcome": "SUCCESS", "count": 2}],
                "record_result": record_result,
            },
            "usage": [
                {
                    "role": "tz_spec",
                    "prompt_tokens": 3,
                    "completion_tokens": 5,
                    "latency_ms": 1.0,
                    "tool_calls": [],
                },
                {
                    "role": "executor-route",
                    "task_key": "sid/EXP-0",
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "latency_ms": 2.0,
                    "tool_calls": [],
                },
                {
                    "role": "report",
                    "model": "glm-4.7",
                    "prompt_tokens": 7,
                    "completion_tokens": 9,
                },
                {
                    # coder job row: no token keys -> not an LLM call
                    "role": "coder",
                    "task_key": "sid/EXP-0",
                    "job_id": "j1",
                    "status": "succeeded",
                    "artifact_ids": ["a1"],
                },
                {
                    # retry note row: no token keys -> not an LLM call
                    "role": "executor-route",
                    "task_key": "sid/EXP-1",
                    "attempt_id": "sid/EXP-1#1",
                    "retryable_error": "boom",
                },
            ],
        },
        ensure_ascii=False,
    )


def test_parse_summary_takes_last_nonempty_line() -> None:
    stdout = f"some warning on stdout\n\n{_summary_line()}\n"
    summary = parse_summary(stdout)
    assert summary["verdict"] == "fulfilled"


def test_parse_summary_non_object_refuses() -> None:
    with pytest.raises(ValueError, match="not a JSON object"):
        parse_summary('"just a string"\n')


def test_stderr_tail_takes_last_line() -> None:
    assert stderr_tail("warning...\ncli refuse: bad flag\n") == "cli refuse: bad flag"
    assert stderr_tail("") == "no stderr output"


def test_result_from_process_success_maps_quality_and_usage(tmp_path: Path) -> None:
    result = result_from_process(
        _task(),
        returncode=0,
        stdout=f"{_summary_line()}\n",
        stderr="",
        study_id="bench-test-config_probe",
    )
    assert isinstance(result, RunnerSuccess)
    roles = [record.role for record in result.usage_recorded]
    # LLM token rows only: tz_spec + executor-route + report lane; coder/retry
    # note rows carry no tokens and are not LLM calls.
    assert roles == ["tz_spec", "executor-route", "report"]
    report = result.usage_recorded[2]
    assert report.usage.latency_ms == 0.0  # lane rows aggregate; latency not declared
    assert report.usage.prompt_tokens == 7
    executor = result.usage_recorded[1]
    assert executor.task_id == "sid/EXP-0"
    assert executor.study_id == "bench-e2e-live_probe"
    quality = result.quality
    assert quality.status == "fulfilled"
    assert quality.exp_tasks == 2
    assert quality.evidence == (EvidenceOutcomeCount(outcome="SUCCESS", count=2),)
    assert quality.record_result == 2


def test_result_from_process_nonfulfilled_verdict_is_observed() -> None:
    result = result_from_process(
        _task(),
        returncode=7,
        stdout=f"{_summary_line(verdict='partial')}\n",
        stderr="",
        study_id="bench-test-config_probe",
    )
    assert isinstance(result, RunnerSuccess)
    assert result.quality.status == "partial"


def test_result_from_process_nonzero_exit_refuses() -> None:
    result = result_from_process(
        _task(),
        returncode=3,
        stdout="",
        stderr="cli refuse: accept_plan refused plan: empty\n",
        study_id="bench-test-config_probe",
    )
    assert isinstance(result, RunnerRefuse)
    assert result.reason_code == "exit_3"
    assert "cli refuse: accept_plan refused plan: empty" in result.detail


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "no json at all\n",
        '{"study_id": "s", "phase": "closed"}\n',  # missing verdict/quality/usage keys
        _summary_line(verdict=None),
        _summary_line(evidence=[{"outcome": "SUCCESS"}]),  # missing count
    ],
)
def test_result_from_process_unreadable_summary_refuses(stdout: str) -> None:
    result = result_from_process(
        _task(), returncode=0, stdout=stdout, stderr="", study_id="bench-test-config_probe"
    )
    assert isinstance(result, RunnerRefuse)
    assert result.reason_code == "summary_unreadable"


# --- e2e: real CLI subprocess over the local fake-LLM HTTP server ---------------


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


@pytest.fixture
def fake_llm_server(tmp_path: Path):
    """Fake OpenAI-compatible server (content-keyed cassette bodies)."""
    run_dir = tmp_path / "run"
    port = _free_port()
    assert fake_llm.main(["fixtures", "--dir", str(run_dir), "--port", str(port)]) == 0
    bodies_dir = run_dir / "bodies"
    server = ThreadingHTTPServer(("127.0.0.1", port), fake_llm._Handler)
    server.cfg = {  # type: ignore[attr-defined]
        "bodies": {
            n: (bodies_dir / f"{n}.json").read_text(encoding="utf-8")
            for n in ("spec", "plan", "exec0", "exec1")
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


@pytest.fixture
def live_config(fake_llm_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LiveRunnerConfig:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "dummy-key-for-fake-llm")
    return LiveRunnerConfig.model_validate(
        _config_dict(
            tmp_path,
            llm_config=str(fake_llm_server / "llm.json"),
            catalog=str(GLM_CATALOG),
            tools_json=str(fake_llm_server / "tools.json"),
            data_root=str(fake_llm_server / "data"),
            subprocess_cwd=str(ROOT),
            timeout_s=300.0,
        )
    )


def test_live_runner_e2e_fake_llm_quality_compare_passes(
    live_config: LiveRunnerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "dummy-key-for-fake-llm")
    task = _task(
        task_id="live_probe",
        prompt="Две задачи: dataset_overview и chemical_space_clustering (fixture text).",
        expected_metrics=ExpectedMetrics(dur_s=0.0, llm_calls=4, tokens=32),
        quality=QualityExpectation(
            status="fulfilled",
            exp_tasks=2,
            evidence=(EvidenceOutcomeCount(outcome="SUCCESS", count=2),),
        ),
    )
    # The full harness path — run_suite calls the runner, wall-times, attaches
    # usage and the exact quality comparison.
    suite = run_suite(
        LiveRunner(live_config),
        (task,),
        system_id="new",
        task_set_id="t",
        task_set_version="1",
    )
    entry = suite.runs[0]
    assert entry.run.kind == "ok", getattr(entry.run, "detail", "")
    run = entry.run
    assert run.quality.status == "fulfilled"
    assert run.quality.exp_tasks == 2
    assert run.quality.evidence == (EvidenceOutcomeCount(outcome="SUCCESS", count=2),)
    assert run.quality.record_result == 2
    roles = [record.role for record in run.usage_recorded]
    assert roles[0] == "tz_spec"
    assert "planner" in roles
    assert "executor-route" in roles
    assert entry.quality is not None and entry.quality.equal is True


def test_live_runner_e2e_refuses_second_run_same_journal(
    live_config: LiveRunnerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COSCI_TEST_LLM_KEY", "dummy-key-for-fake-llm")
    runner = LiveRunner(live_config)
    task = _task(task_id="live_probe")
    first = runner.run(task)
    assert isinstance(first, RunnerSuccess)
    second = runner.run(task)
    assert isinstance(second, RunnerRefuse)
    assert second.reason_code == "study_journal_exists"
