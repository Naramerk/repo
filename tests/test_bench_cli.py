"""Tests for the runner registry and the bench CLI (plan item 14)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bench.cli import main
from bench.errors import BenchConfigError
from bench.runner import BenchRunner
from bench.runners import build_runner, registered_runners

REPO_ROOT = Path(__file__).resolve().parents[1]
HER_SUITE_DIR = REPO_ROOT / "bench" / "tasks"


def test_registry_has_fake_only() -> None:
    assert registered_runners() == ("fake",)


def test_build_runner_fake_satisfies_protocol() -> None:
    assert isinstance(build_runner("fake"), BenchRunner)


@pytest.mark.parametrize("name", ["old", "new"])
def test_build_runner_unregistered_names_typed_refuse(name: str) -> None:
    # Old-system (item 12) and new-system (item 13) adapters wait for the
    # live-run recipe; refusing by name is the correct behavior today.
    with pytest.raises(BenchConfigError, match=f"runner not registered: '{name}'"):
        build_runner(name)


def test_cli_run_fake_over_her_suite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "artifacts"
    code = main(["run", "--suite", str(HER_SUITE_DIR), "--runner", "fake", "--out", str(out_dir)])
    assert code == 0
    payload = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    assert payload["system_id"] == "fake"
    assert payload["task_set_id"] == "her_runs_final_20261023"
    assert len(payload["runs"]) == 10
    assert all(run["kind"] == "ok" for run in payload["runs"])
    usage_lines = (out_dir / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert usage_lines
    assert payload["llm_calls"] == len(usage_lines)
    assert (out_dir / "summary.md").read_text(encoding="utf-8").startswith("# Bench suite run")
    assert "wrote" in capsys.readouterr().out


def test_cli_run_unregistered_runner_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["run", "--suite", str(HER_SUITE_DIR), "--runner", "new", "--out", str(tmp_path / "o")])
    assert code == 2
    err = capsys.readouterr().err
    assert "runner not registered: 'new'" in err
    assert not (tmp_path / "o").exists()


def test_cli_run_missing_manifest_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty-suite"
    empty.mkdir()
    code = main(["run", "--suite", str(empty), "--runner", "fake", "--out", str(tmp_path / "o")])
    assert code == 2
    assert "task set manifest missing" in capsys.readouterr().err


def test_cli_run_out_path_is_file_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    code = main(["run", "--suite", str(HER_SUITE_DIR), "--runner", "fake", "--out", str(blocker)])
    assert code == 2
    assert "not a directory" in capsys.readouterr().err
    assert blocker.read_text(encoding="utf-8") == "x"


def test_cli_requires_all_three_flags() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["run", "--suite", str(HER_SUITE_DIR)])
    assert excinfo.value.code == 2


def test_python_dash_m_bench_run(tmp_path: Path) -> None:
    out_dir = tmp_path / "artifacts"
    proc = subprocess.run(
        [sys.executable, "-m", "bench", "run", "--suite", "bench/tasks", "--runner", "fake", "--out", str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert (out_dir / "result.json").is_file()
    assert (out_dir / "summary.md").is_file()
    assert (out_dir / "usage.jsonl").is_file()


def test_python_dash_m_bench_unregistered_runner(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "bench", "run", "--suite", "bench/tasks", "--runner", "new", "--out", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "runner not registered: 'new'" in proc.stderr
