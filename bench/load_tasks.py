"""Load a versioned task set from YAML (plan item 7).

Fail-closed: every wiring mistake (missing manifest/task/input, malformed YAML,
model violations, manifest-vs-task version mismatch, duplicate ids, file-stem
mismatch) raises a typed :class:`BenchConfigError`. Nothing is skipped silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from bench.errors import BenchConfigError
from bench.models import BenchTask, TaskSetManifest

MANIFEST_FILENAME = "manifest.yaml"


def _read_yaml_mapping(path: Path, what: str) -> dict[str, Any]:
    """Read a YAML file that must exist and contain a mapping; typed refuse otherwise."""
    if not path.is_file():
        raise BenchConfigError(f"{what} missing: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise BenchConfigError(f"{what} is not valid YAML: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BenchConfigError(f"{what} must be a YAML mapping: {path}")
    return raw


def _coerce_tuple_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """YAML lists -> model tuples (bench.models are strict; lists never pass)."""
    coerced = dict(raw)
    if "inputs" in coerced:
        coerced["inputs"] = tuple(coerced["inputs"])
    if "tasks" in coerced:
        coerced["tasks"] = tuple(coerced["tasks"])
    if isinstance(coerced.get("quality"), dict) and "evidence" in coerced["quality"]:
        quality = dict(coerced["quality"])
        quality["evidence"] = tuple(quality["evidence"])
        coerced["quality"] = quality
    return coerced


def load_manifest(suite_dir: Path) -> TaskSetManifest:
    """Load and validate the task-set manifest (``manifest.yaml`` in ``suite_dir``)."""
    path = suite_dir / MANIFEST_FILENAME
    raw = _read_yaml_mapping(path, "task set manifest")
    try:
        return TaskSetManifest.model_validate(_coerce_tuple_fields(raw))
    except ValidationError as exc:
        raise BenchConfigError(f"task set manifest invalid: {path}: {exc}") from exc


def load_task(path: Path) -> BenchTask:
    """Load and validate one task YAML file."""
    raw = _read_yaml_mapping(path, "task file")
    try:
        return BenchTask.model_validate(_coerce_tuple_fields(raw))
    except ValidationError as exc:
        raise BenchConfigError(f"task file invalid: {path}: {exc}") from exc


def load_tasks(suite_dir: Path) -> tuple[BenchTask, ...]:
    """Load the whole task set in manifest order.

    Refuses (typed) on: missing manifest, missing task file, missing declared
    input file, version mismatch between manifest and a task, duplicate task
    ids, and a task file whose stem does not match its ``task_id`` (ids are the
    extract scenario names; the stem convention keeps data findable).
    """
    manifest = load_manifest(suite_dir)
    tasks: list[BenchTask] = []
    seen_ids: set[str] = set()
    for entry in manifest.tasks:
        path = suite_dir / entry
        task = load_task(path)
        if task.task_set_version != manifest.task_set_version:
            raise BenchConfigError(
                f"task set version mismatch: manifest {manifest.task_set_version!r} vs "
                f"{path}: {task.task_set_version!r}"
            )
        if task.task_id in seen_ids:
            raise BenchConfigError(f"duplicate task_id {task.task_id!r} (second occurrence: {path})")
        if path.stem != task.task_id:
            raise BenchConfigError(
                f"task file stem must equal task_id (stem {path.stem!r} != task_id {task.task_id!r}): {path}"
            )
        for input_rel in task.inputs:
            input_path = suite_dir / input_rel
            if not input_path.is_file():
                raise BenchConfigError(f"task {task.task_id!r} declares missing input: {input_path}")
        seen_ids.add(task.task_id)
        tasks.append(task)
    return tuple(tasks)
