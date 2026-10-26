"""bench/lab/driver/webdrive.py — pure helper contract (old webdrive shape)."""

from __future__ import annotations

import argparse
import asyncio
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


def test_ws_stream_url_resume_after_seq() -> None:
    assert (
        wd.ws_stream_url("http://h:1", "st_abc", after=41)
        == "ws://h:1/studies/st_abc/stream?after=41"
    )


def test_collect_events_resumes_after_seq_and_retries_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dial + mid-stream failures retry within budget; reconnect resumes seq.

    F3/F4: the reconnect URL carries ``after=<last seen seq>`` (no journal
    replay -> no duplicate JSONL lines / inflated ``n_events``), and the
    retry loop covers OSError/TimeoutError, not just WebSocketException.
    """
    script: list[object] = [
        ConnectionRefusedError("dial"),  # first connect() call raises
        {"seq": 1, "type": "StudyCreated", "study_id": "st_x"},
        ConnectionResetError("reset"),  # mid-stream drop
        {
            "seq": 2,
            "type": "StudyFinalized",
            "payload": {"verdict": "fulfilled"},
            "study_id": "st_x",
        },
    ]
    urls: list[str] = []

    class FakeWS:
        async def __aenter__(self) -> "FakeWS":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def recv(self) -> str:
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return json.dumps(item)

    class FakeWebsockets:
        class WebSocketException(Exception):
            pass

        @staticmethod
        def connect(url: str, **kwargs: object) -> FakeWS:
            urls.append(url)
            first = len(urls) == 1
            if first and isinstance(script[0], Exception):
                raise script.pop(0)
            return FakeWS()

    monkeypatch.setitem(sys.modules, "websockets", FakeWebsockets)
    monkeypatch.setattr(wd, "_CONNECT_RETRY_SLEEP_S", 0.0)

    out_path = tmp_path / "ev.jsonl"
    args = argparse.Namespace(base_url="http://127.0.0.1:1", study_id="st_x", timeout_s=5.0)
    with out_path.open("w", encoding="utf-8") as events_out:
        events, final_event, timed_out = asyncio.run(wd._collect_events(args, events_out))

    assert [e["seq"] for e in events] == [1, 2]
    assert final_event is not None and wd.verdict_of(final_event) == "fulfilled"
    assert timed_out is False
    assert urls == [
        "ws://127.0.0.1:1/studies/st_x/stream?after=0",  # dial refused
        "ws://127.0.0.1:1/studies/st_x/stream?after=0",  # ev1 then reset
        "ws://127.0.0.1:1/studies/st_x/stream?after=1",  # resume after last seq
    ]
    assert out_path.read_text(encoding="utf-8").count("\n") == 2


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
            "--post-timeout-s",
            "5.0",
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
