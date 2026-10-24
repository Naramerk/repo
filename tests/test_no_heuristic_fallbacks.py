"""Raise tests for schema-v1 baseline: no heuristic fallbacks on remaining modules.

Scoped to events/store/upcasters + domain StudyConfig/Study (catalog/plan/executor
cleanup deferred — those modules match main and are out of ``--vs-main``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from coscientist.core.events.clock import SystemClock
from coscientist.core.events.errors import (
    JournalFormatError,
    JournalLockedError,
    JournalUnavailableError,
)
from coscientist.core.events.events import UnstampedEvent
from coscientist.core.events.upcasters import require_exact_int
from tests.helpers import default_test_hitl_policy, default_test_study_config


def _sqlite_err(cls: type[BaseException], msg: str, code: int) -> BaseException:
    exc = cls(msg)
    exc.sqlite_errorcode = code  # type: ignore[attr-defined]
    return exc


def test_blocker3_require_exact_int_rejects_bool():
    with pytest.raises(JournalFormatError, match="exact int"):
        require_exact_int(True, what="x")


def test_map_sqlite_locked_is_not_truncated():
    """SQLITE_BUSY/LOCKED → JournalLockedError via sqlite_errorcode, not message text."""
    import sqlite3

    from coscientist.core.events.store import _map_sqlite_open_error

    locked = _map_sqlite_open_error(
        _sqlite_err(sqlite3.OperationalError, "x", sqlite3.SQLITE_BUSY)
    )
    assert isinstance(locked, JournalLockedError)
    assert not isinstance(locked, JournalFormatError)

    busy = _map_sqlite_open_error(
        _sqlite_err(sqlite3.OperationalError, "x", sqlite3.SQLITE_LOCKED)
    )
    assert isinstance(busy, JournalLockedError)


def test_map_sqlite_other_code_is_unavailable_not_truncated():
    import sqlite3

    from coscientist.core.events.store import _map_sqlite_open_error

    mapped = _map_sqlite_open_error(
        _sqlite_err(sqlite3.OperationalError, "disk I/O error", sqlite3.SQLITE_IOERR)
    )
    assert isinstance(mapped, JournalUnavailableError)
    assert "truncated" not in str(mapped).lower()


def test_map_sqlite_unknown_error_is_unavailable_not_truncated():
    """Bare sqlite3.Error without a known code must not become 'truncated'."""
    import sqlite3

    from coscientist.core.events.store import _map_sqlite_open_error

    mapped = _map_sqlite_open_error(sqlite3.Error("mystery"))
    assert isinstance(mapped, JournalUnavailableError)
    assert "truncated" not in str(mapped).lower()


def test_map_sqlite_corrupt_stays_format_error():
    import sqlite3

    from coscientist.core.events.store import _map_sqlite_open_error
    from coscientist.core.events.store_db import JOURNAL_STORE_FORMAT

    mapped = _map_sqlite_open_error(
        _sqlite_err(sqlite3.DatabaseError, "file is not a database", sqlite3.SQLITE_NOTADB)
    )
    assert isinstance(mapped, JournalFormatError)
    assert "truncated" in str(mapped).lower() or "unreadable" in str(mapped).lower()
    assert mapped.expected_store_format == JOURNAL_STORE_FORMAT
    assert mapped.found_store_format is None


def test_blocker_store_naive_datetime_errors():
    from coscientist.core.events.store_db import JOURNAL_STORE_FORMAT, iso

    with pytest.raises(JournalFormatError) as ei:
        iso(datetime(2026, 1, 1, 12, 0, 0))
    assert ei.value.expected_store_format == JOURNAL_STORE_FORMAT
    assert ei.value.found_store_format is None
    assert ei.value.store_format == JOURNAL_STORE_FORMAT


def test_journal_format_error_carries_expected_and_found_store_format(tmp_path):
    import sqlite3

    from coscientist.core.events.store import SqliteEventStore
    from coscientist.core.events.store_db import JOURNAL_STORE_FORMAT

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE events(
          seq INTEGER PRIMARY KEY, study_id TEXT, type TEXT, entity_key TEXT,
          idem_key TEXT UNIQUE, payload JSON, ts TEXT, schema_version INT,
          epoch INT, payload_digest TEXT);
        CREATE TABLE lease(
          study_id TEXT PRIMARY KEY, owner TEXT, owner_nonce TEXT,
          epoch INT, expires_at TEXT);
        CREATE TABLE projections(
          table_name TEXT, row_key TEXT, data JSON, PRIMARY KEY(table_name, row_key));
        CREATE TABLE journal_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO events VALUES (1,'s','X','k','i','{}','2026-01-01T00:00:00+00:00',1,1,'abc');
        """
    )
    conn.commit()
    conn.close()
    with pytest.raises(JournalFormatError) as ei:
        SqliteEventStore("s", db, lease_ttl_s=30, clock=SystemClock())
    assert ei.value.expected_store_format == JOURNAL_STORE_FORMAT
    assert ei.value.found_store_format is None
    assert ei.value.store_format == JOURNAL_STORE_FORMAT


def test_journal_format_error_mismatch_sets_found_store_format(tmp_path):
    import sqlite3

    from coscientist.core.events.store import SqliteEventStore
    from coscientist.core.events.store_db import JOURNAL_STORE_FORMAT

    db = tmp_path / "mismatch.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE events(
          seq INTEGER PRIMARY KEY, study_id TEXT, type TEXT, entity_key TEXT,
          idem_key TEXT UNIQUE, payload JSON, ts TEXT, schema_version INT,
          epoch INT, payload_digest TEXT);
        CREATE TABLE lease(
          study_id TEXT PRIMARY KEY, owner TEXT, owner_nonce TEXT,
          epoch INT, expires_at TEXT);
        CREATE TABLE projections(
          table_name TEXT, row_key TEXT, data JSON, PRIMARY KEY(table_name, row_key));
        CREATE TABLE journal_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO journal_meta(key, value) VALUES ('store_format', '99');
        """
    )
    conn.commit()
    conn.close()
    with pytest.raises(JournalFormatError) as ei:
        SqliteEventStore("s", db, lease_ttl_s=30, clock=SystemClock())
    assert ei.value.expected_store_format == JOURNAL_STORE_FORMAT
    assert ei.value.found_store_format == 99


def test_unstamped_event_rejects_unknown_event_type():
    """``type: EventType`` Literal contract — unknown strings fail validation."""
    with pytest.raises(ValidationError):
        UnstampedEvent(
            study_id="s",
            type="NotARealEventType",  # type: ignore[arg-type]
            entity_key="k",
            idem_key="i",
            payload={},
            ts=datetime.now(UTC),
            schema_version=1,
        )


def test_c4_study_config_missing_fields_validation_error():
    from coscientist.core.execution.domain import StudyConfig

    with pytest.raises(ValidationError):
        StudyConfig()  # type: ignore[call-arg]


def test_c4_study_missing_domain_fields_validation_error():
    from coscientist.core.execution.domain import Study

    with pytest.raises(ValidationError):
        Study(
            study_id="s",
            profile="p",
            request="r",
            catalog_version="1",
            hitl_policy=default_test_hitl_policy(),
            deadline=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )


def test_blocker_default_study_domain_fields_required():
    """Study-domain models raise when inventing Field defaults are absent."""
    from coscientist.core.execution.domain import Study, StudyConfig

    with pytest.raises(ValidationError):
        StudyConfig()  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Study(
            study_id="s",
            profile="p",
            request="r",
            catalog_version="1",
            hitl_policy=default_test_hitl_policy(),
            deadline=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )


def test_blocker_default_no_default_star_in_domain():
    import coscientist.core.execution.domain as domain_mod

    assert not any(n.startswith("_DEFAULT_") for n in dir(domain_mod))
    assert not any(n.startswith("DEFAULT_") and n.isupper() for n in dir(domain_mod) if n.startswith("DEFAULT_"))


@pytest.mark.asyncio
async def test_c4_create_study_requires_config_hitl_deadline_typeerror():
    from coscientist.core.plan.lifecycle import LifecycleMixin

    class _LC(LifecycleMixin):
        def __init__(self) -> None:
            self.store = SimpleNamespace(study_id=None)
            self.clock = SimpleNamespace(now=lambda: datetime.now(UTC))

    with pytest.raises(TypeError, match="StudyConfig"):
        await _LC().create_study(
            profile="p",
            request="r",
            catalog_version="1",
            config=None,  # type: ignore[arg-type]
            hitl_policy=default_test_hitl_policy(),
            deadline=datetime.now(UTC) + timedelta(hours=1),
            min_plan_cost=0,
        )
    with pytest.raises(TypeError, match="HitlPolicy"):
        await _LC().create_study(
            profile="p",
            request="r",
            catalog_version="1",
            config=default_test_study_config(),
            hitl_policy=None,  # type: ignore[arg-type]
            deadline=datetime.now(UTC) + timedelta(hours=1),
            min_plan_cost=0,
        )


def test_replan_budget_s_uses_required_reserve():
    from coscientist.core.execution.domain import Study

    study = Study(
        study_id="s",
        profile="p",
        request="r",
        catalog_version="1",
        hitl_policy=default_test_hitl_policy(),
        deadline=datetime.now(UTC) + timedelta(hours=1),
        created_at=datetime.now(UTC),
        **{
            k: getattr(default_test_study_config(), k)
            for k in (
                "finalize_reserve_s",
                "confirmations_needed",
                "refutations_needed",
                "refutation_priority",
                "supported_needed",
                "aggregate",
                "evalue_alpha",
                "max_expansions",
                "max_human_expansions",
                "replan_reserve",
                "max_tasks",
                "max_depth",
                "plan_timeout_s",
                "max_foreach_children",
                "max_loop_iter",
                "max_task_budget_share",
                "max_proposals",
            )
        },
    )
    budget = study.replan_budget_s()
    assert budget >= 0.0
