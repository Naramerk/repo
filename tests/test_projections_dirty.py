"""Dirty-row tracking for apply_event (fsync-critical path)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.projections import _DIRTY_KEYS, _put, apply_event, apply_to_map


def test_put_records_dirty_keys_when_tracking():
    proj: dict[str, dict[str, dict[str, Any]]] = {}
    dirty: set[tuple[str, str]] = set()
    token = _DIRTY_KEYS.set(dirty)
    try:
        _put(proj, "attempts", "a1", {"attempt_id": "a1", "n": 1})
        _put(proj, "attempts", "a1", {"attempt_id": "a1", "n": 2})
        _put(proj, "tasks", "t1", {"task_id": "t1"})
    finally:
        _DIRTY_KEYS.reset(token)
    assert dirty == {("attempts", "a1"), ("tasks", "t1")}
    assert _DIRTY_KEYS.get() is None


def test_apply_event_upserts_only_dirty_rows():
    """Invariant: unchanged loaded rows are not rewritten to SQLite."""

    class _FakeStore:
        def __init__(self) -> None:
            self.tables: dict[str, dict[str, dict[str, Any]]] = {
                "attempts": {
                    "a1": {"attempt_id": "a1", "route": "old"},
                    "a2": {"attempt_id": "a2", "route": "keep"},
                }
            }
            self.upserts: list[tuple[str, str, dict[str, Any]]] = []

        def _get_projection_write(self, table: str) -> list[dict[str, Any]]:
            return list(self.tables.get(table, {}).values())

        def _upsert_projection_write(
            self, table: str, key: str, data: dict[str, Any]
        ) -> None:
            self.upserts.append((table, key, data))
            self.tables.setdefault(table, {})[key] = data

    store = _FakeStore()
    ev = EventEnvelope(
        study_id="st",
        type="RouteReturned",
        entity_key="a1",
        idem_key=make_idem("RouteReturned", "a1"),
        payload={"attempt_id": "a1", "route": "new"},
        ts=datetime.now(UTC),
        schema_version=1,
    )
    apply_event(store, ev)
    assert store.upserts == [
        ("attempts", "a1", store.tables["attempts"]["a1"]),
    ]
    assert store.tables["attempts"]["a2"]["route"] == "keep"


def test_apply_to_map_without_tracking_leaves_context_clean():
    proj: dict[str, dict[str, dict[str, Any]]] = {}
    ev = EventEnvelope(
        study_id="st",
        type="StudyCreated",
        entity_key="st",
        idem_key=make_idem("StudyCreated", "st"),
        payload={"study_id": "st"},
        ts=datetime.now(UTC),
        schema_version=1,
    )
    apply_to_map(proj, ev)
    assert _DIRTY_KEYS.get() is None
    assert "st" in proj.get("studies", {})
