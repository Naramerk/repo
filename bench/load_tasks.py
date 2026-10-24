"""Load versioned task-set configs. Tasks are never hardcoded in code."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from bench.errors import BenchConfigError
from bench.models import BenchTask, TaskSetManifest


def _require_mapping(data: Any, *, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise BenchConfigError(f"{path}: root must be a mapping, got {type(data).__name__}")
    return data


def load_manifest(path: Path) -> TaskSetManifest:
    if not path.is_file():
        raise BenchConfigError(f"task-set manifest not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    mapping = _require_mapping(raw, path=path)
    try:
        return TaskSetManifest.model_validate(mapping)
    except ValidationError as exc:
        raise BenchConfigError(f"{path}: invalid manifest: {exc}") from exc


def load_task_file(path: Path) -> BenchTask:
    if not path.is_file():
        raise BenchConfigError(f"task file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    mapping = _require_mapping(raw, path=path)
    try:
        return BenchTask.model_validate(mapping)
    except ValidationError as exc:
        raise BenchConfigError(f"{path}: invalid task: {exc}") from exc


def load_task_set(manifest_path: Path) -> tuple[TaskSetManifest, tuple[BenchTask, ...]]:
    """Load manifest and every listed task file (paths relative to the manifest dir)."""
    manifest = load_manifest(manifest_path)
    base = manifest_path.parent
    tasks: list[BenchTask] = []
    seen_ids: set[str] = set()
    for rel in manifest.tasks:
        if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise BenchConfigError(
                f"{manifest_path}: task path must be a relative file under the "
                f"manifest directory (got {rel!r})"
            )
        task_path = base / rel
        task = load_task_file(task_path)
        if task.task_set_version != manifest.task_set_version:
            raise BenchConfigError(
                f"{task_path}: task_set_version {task.task_set_version!r} != "
                f"manifest {manifest.task_set_version!r}"
            )
        if task.task_id in seen_ids:
            raise BenchConfigError(f"{manifest_path}: duplicate task_id {task.task_id!r}")
        seen_ids.add(task.task_id)
        tasks.append(task)
    if not tasks:
        raise BenchConfigError(f"{manifest_path}: task list resolved empty")
    return manifest, tuple(tasks)
