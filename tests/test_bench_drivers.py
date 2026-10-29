"""Unit tests for the driver lanes (plan gap 3): config bounds, journal
observation, argv/POST-body mappings, and the LiveRunner branch/preflight.

The e2e lanes live in test_bench_driver_web_e2e.py (uvicorn subprocess) and
test_bench_driver_recovery_e2e.py (SIGKILL + continue, slow).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from bench.clilane import build_run_argv
from bench.drivers import Driver, RecoveryDriver, WebDriver
from bench.drivers.journal import count_marker_events, observe_quality, read_journal_events
from bench.drivers.recovery import build_continue_argv, observed_timeout
from bench.drivers.web import run_body
from bench.models import BenchTask, EvidenceOutcomeCount, ExpectedMetrics, QualityExpectation, RunnerSuccess

# --- journal observation (shared fold; parity with the summary fold is pinned
# --- by the web e2e on a real closed study) ------------------------------------


def _event(etype: str, payload: dict[str, Any], seq: int = 1) -> dict[str, Any]:
    return {"seq": seq, "type": etype, "entity_key": f"s/{etype}", "payload": payload}


def test_observe_quality_experiments_tallies_and_verdict() -> None:
    events = [
        _event("StudyCreated", {"study_id": "s"}),
        _event("TaskCreated", {"task_id": "s/SPEC", "kind": "spec"}),
        _event("TaskCreated", {"task_id": "s/EXP-0", "kind": "experiment"}),
        _event("TaskCreated", {"task_id": "s/EXP-1", "kind": "experiment"}),
        _event(
            "ResultRecorded",
            {"result_id": "r1", "attempt_id": "a1", "task_id": "s/EXP-0", "outcome": "SUCCESS"},
            seq=5,
        ),
        # The runtime fold double-keys rows (result_id + attempt_id); the
        # observer dedups by result_id — a replayed row counts once.
        _event(
            "ResultRecorded",
            {"result_id": "r1", "attempt_id": "a1", "task_id": "s/EXP-0", "outcome": "SUCCESS"},
            seq=6,
        ),
        _event(
            "ResultRecorded",
            {"result_id": "r2", "attempt_id": "a2", "task_id": "s/EXP-1", "outcome": "FAILED"},
            seq=7,
        ),
        # Stage results are not experiment evidence.
        _event(
            "ResultRecorded",
            {"result_id": "r3", "attempt_id": "a3", "task_id": "s/SPEC", "outcome": "SUCCESS"},
            seq=8,
        ),
        _event("StudyFinalized", {"verdict": "fulfilled"}, seq=9),
    ]
    observed = observe_quality(events)
    assert observed["exp_tasks"] == 2
    assert observed["evidence"] == [
        {"outcome": "FAILED", "count": 1},
        {"outcome": "SUCCESS", "count": 1},
    ]
    assert observed["record_result"] == 2
    assert observed["verdict"] == "fulfilled"
    assert observed["finalized"] == 1


def test_observe_quality_open_study_has_no_verdict() -> None:
    events = [_event("TaskCreated", {"task_id": "s/EXP-0", "kind": "experiment"})]
    observed = observe_quality(events)
    assert observed["verdict"] is None
    assert observed["finalized"] == 0
    assert observed["exp_tasks"] == 1
    assert observed["evidence"] == []


def _mk_journal(db: Path, rows: list[tuple[int, str, str, dict[str, Any]]]) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE events(seq INTEGER PRIMARY KEY, study_id TEXT NOT NULL, type TEXT NOT NULL, "
            "entity_key TEXT NOT NULL, idem_key TEXT NOT NULL UNIQUE, payload JSON NOT NULL, ts TEXT NOT NULL, "
            "schema_version INT NOT NULL, epoch INT NOT NULL, payload_digest TEXT NOT NULL)"
        )
        for seq, etype, entity_key, payload in rows:
            conn.execute(
                "INSERT INTO events(seq, study_id, type, entity_key, idem_key, payload, ts, schema_version, "
                "epoch, payload_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (seq, "study", etype, entity_key, f"id{seq}", json.dumps(payload), "t", 1, 1, "d"),
            )
        conn.commit()
    finally:
        conn.close()


def test_read_and_count_marker_events(tmp_path: Path) -> None:
    db = tmp_path / "study" / "events.db"
    _mk_journal(
        db,
        [
            (1, "StudyCreated", "study", {}),
            (2, "ResultRecorded", "study/EXP-0#1", {"result_id": "r1"}),
            (3, "ResultRecorded", "study/SPEC#1", {"result_id": "r2"}),
        ],
    )
    events = read_journal_events(db, "study")
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert events[1]["payload"] == {"result_id": "r1"}
    assert count_marker_events(db, "study", event_type="ResultRecorded", entity_contains="/EXP-") == 1
    assert count_marker_events(db, "study", event_type="ResultRecorded", entity_contains="study/") == 2
    assert count_marker_events(db, "study", event_type="StudyFinalized", entity_contains="EXP") == 0


def test_count_marker_events_missing_db_is_zero(tmp_path: Path) -> None:
    absent = tmp_path / "absent" / "events.db"
    assert count_marker_events(absent, "s", event_type="ResultRecorded", entity_contains="X") == 0
    assert read_journal_events(absent, "s") == []


def test_count_marker_events_treats_schemaless_db_as_pre_db(tmp_path: Path) -> None:
    # The writer's sqlite3.connect creates the db file BEFORE the schema
    # commits (SqliteEventStore.__init__), so the driver's 0.2s kill-marker
    # poll can see a file with no events table yet — the documented pre-db
    # state (0), not "no such table: events".
    db = tmp_path / "study" / "events.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    assert count_marker_events(db, "s", event_type="ResultRecorded", entity_contains="X") == 0


def test_read_journal_events_schemaless_db_raises(tmp_path: Path) -> None:
    # read_journal_events runs only after the run has ended (recovery.py
    # _drive/_continue_phase), where a file without the events schema is a
    # real corruption, not the poll's creation window — fail closed with the
    # raw sqlite3 error instead of reporting an empty journal.
    db = tmp_path / "study" / "events.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    with pytest.raises(sqlite3.OperationalError, match="no such table: events"):
        read_journal_events(db, "s")


# --- config driver bounds -------------------------------------------------------


def _config_dict(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    from tests.test_bench_live_runner import _config_dict as base

    return base(tmp_path, **overrides)


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"web_port": 0}, "web_port must be 1..65535"),
        ({"web_port": 70000}, "web_port must be 1..65535"),
        ({"web_stream_poll_interval_s": 0.0}, "web_stream_poll_interval_s must be positive"),
        ({"web_run_wait_s": 0.0}, "web_run_wait_s must be positive"),
        ({"web_post_timeout_s": 30.0}, "web_post_timeout_s must be strictly above web_run_wait_s"),
        ({"web_stream_timeout_s": 0.0}, "web_stream_timeout_s must be positive"),
        ({"web_connect_retries": -1}, "web_connect_retries must be >= 0"),
        ({"recovery_kill_event_type": ""}, "recovery_kill_event_type must be a non-empty"),
        ({"recovery_kill_entity_contains": ""}, "recovery_kill_entity_contains must be a non-empty"),
        ({"recovery_kill_wait_s": 0.0}, "recovery_kill_wait_s must be positive"),
        ({"recovery_lease_wait_s": -1.0}, "recovery_lease_wait_s must be >= 0"),
    ],
)
def test_config_driver_bounds_refuse(tmp_path: Path, overrides: dict[str, Any], fragment: str) -> None:
    from bench.live_runner import LiveRunnerConfig

    with pytest.raises(ValidationError, match=fragment.replace("(", "\\(").replace(")", "\\)")):
        LiveRunnerConfig.model_validate(_config_dict(tmp_path, **overrides))


def test_driver_protocol_satisfied() -> None:
    assert isinstance(WebDriver(), Driver)
    assert isinstance(RecoveryDriver(), Driver)


# --- mechanical mappings: POST body + continue argv -----------------------------


def _task() -> BenchTask:
    return BenchTask.model_validate(
        {
            "task_id": "t",
            "task_set_version": "1",
            "prompt": "prompt text",
            "inputs": (),
            "driver": None,
            "expected_metrics": ExpectedMetrics(dur_s=1.0, llm_calls=1, tokens=8),
            "quality": QualityExpectation(
                status="fulfilled",
                exp_tasks=1,
                evidence=(EvidenceOutcomeCount(outcome="SUCCESS", count=1),),
            ),
        }
    )


def test_web_run_body_maps_every_config_field(tmp_path: Path) -> None:
    from bench.live_runner import LiveRunnerConfig

    config = LiveRunnerConfig.model_validate(_config_dict(tmp_path))
    body = run_body(config, "prompt text", (Path("/abs/input.csv"),))
    # Every RunStudyBody field, verbatim from the shared config (no defaults).
    assert body["task_text"] == "prompt text"
    assert body["owner"] == "bench-test"
    assert body["deadline_s"] == 600.0
    assert body["lease_ttl_s"] == 60.0
    assert body["catalog_version"] == 4
    assert body["catalog_path"] == str(tmp_path / "catalog.json")
    assert body["profile"] == "research-glm"
    assert body["tool_catalog_version"] == 2
    assert body["llm_config_path"] == str(tmp_path / "llm.json")
    assert body["planner_model"] == "glm-4.7"
    assert body["executor_model"] == "glm-4.7"
    assert body["tz_spec_model"] == "glm-4.7"
    assert body["tools_json_path"] == str(tmp_path / "tools.json")
    assert body["mcp_tool_bundle_path"] is None
    assert body["tool_call_args_path"] is None
    assert body["coder_backend"] is None
    assert body["coder_jobs_path"] is None
    assert body["wired_roles"] == []
    assert body["task_input_paths"] == ["/abs/input.csv"]
    assert body["require_hypotheses"] is False
    assert body["confirmations_needed"] == 1
    assert body["refutations_needed"] == 1
    assert body["refutation_priority"] is False
    assert body["supported_needed"] == 2
    assert body["max_expansions"] == 0
    assert body["finalize_reserve_s"] == 30
    assert body["rng_seed"] == 0
    assert body["max_llm_calls"] == 40
    assert body["max_tokens"] == 1000000
    assert body["max_tool_calls"] == 20
    assert body["max_ticks"] == 20
    assert body["hitl_mode"] == "headless"
    # The POST body accepts the exact RunStudyBody contract (strict validation).
    from coscientist.api.run import RunStudyBody

    RunStudyBody.model_validate(body)


def test_continue_argv_maps_continue_flags_not_run_flags(tmp_path: Path) -> None:
    from bench.live_runner import LiveRunnerConfig

    config = LiveRunnerConfig.model_validate(_config_dict(tmp_path))
    argv = build_continue_argv(config, data_root=Path("/data"), study_id="s")
    joined = " ".join(argv)
    for fragment in (
        "-m coscientist.cli continue /data",
        "--study-id s",
        f"--catalog {tmp_path / 'catalog.json'}",
        "--catalog-version 4",
        "--profile research-glm",
        "--tool-catalog-version 2",
        "--owner bench-test",
        f"--llm-config {tmp_path / 'llm.json'}",
        "--planner-model glm-4.7",
        "--executor-model glm-4.7",
        "--tz-spec-model glm-4.7",
        f"--tools-json {tmp_path / 'tools.json'}",
        "--lease-ttl-s 60.0",
        "--lease-wait-s 2.0",
        "--rng-seed 0",
        "--max-llm-calls 40",
        "--max-tokens 1000000",
        "--max-tool-calls 20",
        "--max-ticks 20",
    ):
        assert fragment in joined, fragment
    # Study-creation flags are journaled and immutable — continue has no such
    # flags, and the argv must not invent them.
    for absent in ("--deadline-s", "--hitl-mode", "--wired-roles", "--require-hypotheses", "--task-input"):
        assert absent not in joined


def test_continue_argv_mcp_path(tmp_path: Path) -> None:
    from bench.live_runner import LiveRunnerConfig

    config = LiveRunnerConfig.model_validate(
        _config_dict(
            tmp_path,
            tools_json=None,
            mcp_tool_bundle="/x/bundle.json",
            mcp_tool_call_args="/x/args.json",
        )
    )
    argv = build_continue_argv(config, data_root=Path("/data"), study_id="s")
    joined = " ".join(argv)
    assert "--mcp-tool-bundle /x/bundle.json" in joined
    assert "--tool-call-args /x/args.json" in joined
    assert "--tools-json" not in joined


# --- honest observed outcomes ----------------------------------------------------


def test_observed_timeout_reports_partial_journal(tmp_path: Path) -> None:
    events = [
        _event("TaskCreated", {"task_id": "s/EXP-0", "kind": "experiment"}),
        _event(
            "ResultRecorded",
            {"result_id": "r1", "attempt_id": "a1", "task_id": "s/EXP-0", "outcome": "SUCCESS"},
        ),
    ]
    result = observed_timeout(events)
    assert isinstance(result, RunnerSuccess)
    assert result.quality.status == "timeout"
    assert result.quality.exp_tasks == 1
    assert result.quality.evidence == (EvidenceOutcomeCount(outcome="SUCCESS", count=1),)
    assert result.quality.record_result == 1
    # Usage is genuinely unobservable on a killed run — never estimated.
    assert result.usage_recorded == ()


def test_recovery_run_argv_is_the_shared_cli_mapping(tmp_path: Path) -> None:
    from bench.live_runner import LiveRunnerConfig

    config = LiveRunnerConfig.model_validate(_config_dict(tmp_path))
    # The recovery driver must start the exact same run argv as the CLI lane.
    argv_a = build_run_argv(config, task_file=Path("/t.txt"), study_id="s", input_paths=())
    assert argv_a[3] == "run"
    assert "--study-id" in argv_a
