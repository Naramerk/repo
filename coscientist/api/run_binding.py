"""Durable transport identity for one owner across its replan journals."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from coscientist.api.errors import StudyRunRefuse
from coscientist.api.idempotency import atomic_write
from coscientist.api.journal import safe_study_component, study_path
from coscientist.api.views import require_study_id


def record_active_journal(root: Path, study_id: str, active: str) -> None:
    sid = require_study_id(study_id)
    active = safe_study_component(active)
    study_path(root, active)
    atomic_write(_binding_directory(root) / f"{sid}.json", {"study_id": sid, "active_study_id": active})


def _binding_directory(root: Path) -> Path:
    directory = root / "api-runs"
    if directory.is_symlink() or directory.resolve().parent != root.resolve():
        raise StudyRunRefuse("run_binding_unsafe")
    return directory


def recorded_run_binding(root: Path, study_id: str) -> tuple[str, str] | None:
    """Read identity only; journal events remain the scientific authority."""
    sid = safe_study_component(study_id)
    for owner, active in recorded_run_bindings(root):
        if sid in (owner, active):
            return owner, active
    return None


def recorded_run_bindings(root: Path) -> Iterator[tuple[str, str]]:
    """Validated root/active identities; no inference from journal name suffixes."""
    directory = _binding_directory(root)
    for path in directory.glob("*.json"):
        if path.is_symlink():
            raise StudyRunRefuse("run_binding_unsafe")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StudyRunRefuse("run_binding_unreadable") from exc
        if not isinstance(data, dict) or set(data) != {"study_id", "active_study_id"}:
            raise StudyRunRefuse("run_binding_invalid")
        owner = require_study_id(data["study_id"])
        active = safe_study_component(data["active_study_id"])
        study_path(root, active)
        if path.name != f"{owner}.json":
            raise StudyRunRefuse("run_binding_invalid")
        yield owner, active
