"""bench/lab/driver/webdrive.py — pure helper contract (old webdrive shape)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_webdrive() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "bench" / "lab" / "driver" / "webdrive.py"
    spec = importlib.util.spec_from_file_location("webdrive_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wd = _load_webdrive()


def test_is_final_event_only_study_finalized() -> None:
    assert wd.is_final_event({"type": "StudyFinalized", "payload": {"verdict": "fulfilled"}})
    assert not wd.is_final_event({"type": "StudyCreated", "payload": {}})
    assert not wd.is_final_event({"type": "TaskDone", "payload": {}})


def test_verdict_of_final_and_non_final() -> None:
    assert wd.verdict_of({"type": "StudyFinalized", "payload": {"verdict": "fulfilled"}}) == "fulfilled"
    assert wd.verdict_of({"type": "StudyFinalized", "payload": {}}) is None
    assert wd.verdict_of({"type": "StudyCreated", "payload": {"verdict": "fulfilled"}}) is None


def test_run_body_merges_config_and_task_text() -> None:
    body = wd.run_body({"profile": "research", "owner": "ops"}, "do the study")
    assert body["task_text"] == "do the study"
    assert body["profile"] == "research"
    assert body["owner"] == "ops"


def test_ws_stream_url_scheme_and_path() -> None:
    assert (
        wd.ws_stream_url("http://127.0.0.1:8421", "st_abc")
        == "ws://127.0.0.1:8421/studies/st_abc/stream?after=0"
    )
    assert (
        wd.ws_stream_url("https://api.example/", "st_abc")
        == "wss://api.example/studies/st_abc/stream?after=0"
    )


def test_build_summary_shape() -> None:
    events = [{"type": "StudyCreated"}, {"type": "StudyFinalized", "payload": {"verdict": "fulfilled"}}]
    final_event = events[-1]
    run_status = {
        "study_id": "st_abc",
        "run_status": "done",
        "summary": {"study_id": "st_abc", "phase": "closed", "verdict": "fulfilled"},
    }
    summary = wd.build_summary(
        study_id="st_abc",
        elapsed_s=1.2349,
        events=events,
        final_event=final_event,
        run_status=run_status,
    )
    assert summary["study_id"] == "st_abc"
    assert summary["elapsed_s"] == 1.235
    assert summary["n_events"] == 2
    assert summary["final"]["verdict"] == "fulfilled"
    assert summary["final"]["run_status"] == "done"
    assert summary["final"]["summary"]["phase"] == "closed"
    assert json.dumps(summary)  # JSON-serializable end to end


def test_build_summary_without_final_event() -> None:
    summary = wd.build_summary(
        study_id="st_abc",
        elapsed_s=0.5,
        events=[{"type": "StudyCreated"}],
        final_event=None,
        run_status={"study_id": "st_abc", "run_status": "running"},
    )
    assert summary["n_events"] == 1
    assert summary["final"] == {"run_status": "running"}


def test_main_refuses_empty_task_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task = tmp_path / "task.txt"
    task.write_text("   \n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    code = wd.main(
        [
            "--base-url",
            "http://127.0.0.1:1",
            "--study-id",
            "st_x",
            "--task-file",
            str(task),
            "--config",
            str(config),
            "--timeout-s",
            "1.0",
            "--events-out",
            str(tmp_path / "ev.jsonl"),
            "--summary-out",
            str(tmp_path / "sum.json"),
            "--connect-retries",
            "0",
        ]
    )
    assert code == wd.EXIT_INPUT_REFUSED
    assert "empty_task_file" in capsys.readouterr().err
