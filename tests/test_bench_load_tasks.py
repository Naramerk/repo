"""Unit tests for bench.load_tasks (plan item 7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from bench.errors import BenchConfigError
from bench.load_tasks import load_manifest, load_task, load_tasks

HER_SUITE_DIR = Path(__file__).resolve().parents[1] / "bench" / "tasks"
HER_SCENARIO_ORDER = (
    "two_tasks",
    "hypothesis_check",
    "borshevik",
    "alembic_nomcp",
    "microfluidics",
    "article_small",
    "web",
    "recovery",
    "file_handoff",
    "recovery2",
)


def _task_obj(task_id: str, version: str = "1", inputs: list[str] | None = None) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_set_version": version,
        "prompt": f"prompt for {task_id}",
        "inputs": inputs or [],
        "expected_metrics": {"dur_s": 1.0, "llm_calls": 1, "tokens": 10},
        "quality": {
            "status": "fulfilled",
            "exp_tasks": 1,
            "evidence": [{"outcome": "success", "count": 1}],
        },
    }


def _write_suite(
    suite_dir: Path,
    tasks: dict[str, dict[str, Any]],
    *,
    task_set_id: str = "tmp_set",
    version: str = "1",
    manifest_tasks: list[str] | None = None,
) -> Path:
    suite_dir.mkdir(parents=True, exist_ok=True)
    manifest_entries = manifest_tasks if manifest_tasks is not None else [f"{k}.yaml" for k in tasks]
    (suite_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "task_set_id": task_set_id,
                "task_set_version": version,
                "tasks": manifest_entries,
            }
        ),
        encoding="utf-8",
    )
    for name, obj in tasks.items():
        task_path = suite_dir / f"{name}.yaml"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(yaml.safe_dump(obj), encoding="utf-8")
    return suite_dir


def test_loads_her_suite_in_manifest_order() -> None:
    tasks = load_tasks(HER_SUITE_DIR)
    assert tuple(t.task_id for t in tasks) == HER_SCENARIO_ORDER
    assert all(t.task_set_version == "1" for t in tasks)


def test_her_suite_drivers_and_inputs_resolve() -> None:
    tasks = {t.task_id: t for t in load_tasks(HER_SUITE_DIR)}
    assert tasks["web"].driver == "web"
    assert tasks["recovery"].driver == "recovery"
    assert tasks["recovery2"].driver == "recovery"
    assert tasks["two_tasks"].driver is None
    for task in tasks.values():
        for input_rel in task.inputs:
            assert (HER_SUITE_DIR / input_rel).is_file()


def test_load_task_single_file(tmp_path: Path) -> None:
    suite = _write_suite(tmp_path, {"one": _task_obj("one")})
    task = load_task(suite / "one.yaml")
    assert task.task_id == "one"
    assert task.inputs == ()
    assert task.quality.evidence[0].outcome == "success"


def test_load_manifest_ok(tmp_path: Path) -> None:
    suite = _write_suite(tmp_path, {"one": _task_obj("one")})
    manifest = load_manifest(suite)
    assert manifest.task_set_id == "tmp_set"
    assert manifest.tasks == ("one.yaml",)


def test_loads_tmp_suite_with_inputs(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "in.csv").write_text("a\n", encoding="utf-8")
    suite = _write_suite(tmp_path, {"one": _task_obj("one", inputs=["data/in.csv"])})
    tasks = load_tasks(suite)
    assert tasks[0].inputs == ("data/in.csv",)


def test_missing_manifest_refuses(tmp_path: Path) -> None:
    with pytest.raises(BenchConfigError, match="task set manifest missing"):
        load_tasks(tmp_path)


def test_missing_task_file_refuses(tmp_path: Path) -> None:
    suite = _write_suite(tmp_path, {"one": _task_obj("one")}, manifest_tasks=["one.yaml", "two.yaml"])
    with pytest.raises(BenchConfigError, match=r"task file missing: .*two\.yaml"):
        load_tasks(suite)


def test_version_mismatch_refuses(tmp_path: Path) -> None:
    suite = _write_suite(tmp_path, {"one": _task_obj("one", version="2")})
    with pytest.raises(BenchConfigError, match="task set version mismatch: manifest '1' vs"):
        load_tasks(suite)


def test_missing_declared_input_refuses(tmp_path: Path) -> None:
    suite = _write_suite(tmp_path, {"one": _task_obj("one", inputs=["data/absent.csv"])})
    with pytest.raises(BenchConfigError, match=r"task 'one' declares missing input"):
        load_tasks(suite)


def test_stem_mismatch_refuses(tmp_path: Path) -> None:
    suite = _write_suite(tmp_path, {"a": _task_obj("not_a")})
    with pytest.raises(BenchConfigError, match=r"task file stem must equal task_id"):
        load_tasks(suite)


def test_duplicate_task_id_refuses(tmp_path: Path) -> None:
    # Same task_id in two distinct subdirectories: both stems match, paths unique,
    # so only the duplicate-id guard catches it.
    suite = _write_suite(
        tmp_path,
        {"x/a": _task_obj("a"), "y/a": _task_obj("a")},
        manifest_tasks=["x/a.yaml", "y/a.yaml"],
    )
    with pytest.raises(BenchConfigError, match=r"duplicate task_id 'a'"):
        load_tasks(suite)


def test_manifest_not_mapping_refuses(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "manifest.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(BenchConfigError, match="task set manifest must be a YAML mapping"):
        load_manifest(tmp_path)


def test_malformed_manifest_yaml_refuses(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "manifest.yaml").write_text("a: [unclosed\n", encoding="utf-8")
    with pytest.raises(BenchConfigError, match="task set manifest is not valid YAML"):
        load_manifest(tmp_path)


def test_malformed_task_yaml_refuses(tmp_path: Path) -> None:
    suite = _write_suite(tmp_path, {"one": _task_obj("one")})
    (suite / "one.yaml").write_text("task_id: [unclosed\n", encoding="utf-8")
    with pytest.raises(BenchConfigError, match="task file is not valid YAML"):
        load_tasks(suite)


def test_task_model_violation_refuses(tmp_path: Path) -> None:
    obj = _task_obj("one")
    obj["driver"] = "cli"
    suite = _write_suite(tmp_path, {"one": obj})
    with pytest.raises(BenchConfigError, match="task file invalid"):
        load_tasks(suite)
