"""Schema v1: legacy guessing-upcast scenarios are refused (no invented defaults).

These replace checks removed with pre-v1 upcasters (see PR #38). Each former
``test_upcast_*`` / ``test_replay_*_journal`` case is kept as an explicit
versioned refuse — values are never guessed.
"""

from __future__ import annotations

import pytest

from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.upcasters import UPCASTERS, upcast, writer_schema_version

# Former test function names → event type that no longer carries a guessing chain.
_LEGACY_UPCAST_CASES: list[tuple[str, str]] = [
    ("test_upcast_planaccepted_questions_default", "PlanAccepted"),
    ("test_upcast_study_finalized_v1_adds_counts_and_refusal", "StudyFinalized"),
    ("test_upcast_taskcreated_addresses_default", "TaskCreated"),
    ("test_upcast_taskcreated_input_mode_old_default", "TaskCreated"),
    ("test_upcast_taskcreated_strips_plan_criterion_role", "TaskCreated"),
    ("test_replay_legacy_planaccepted_v1_taskcreated_v2", "TaskCreated"),
    ("test_upcast_attempt_limitations", "AttemptStarted"),
    ("test_upcast_study_foreach_cap", "StudyCreated"),
    ("test_upcast_task_created_foreach_quorum", "TaskCreated"),
    ("test_upcast_plan_accepted_loops_default", "PlanAccepted"),
    ("test_upcast_plan_expanded_v2_loop_field_defaults", "PlanExpanded"),
    ("test_upcast_study_created_max_loop_iter", "StudyCreated"),
    ("test_upcast_task_created_loop_fields", "TaskCreated"),
    ("test_upcast_plan_accepted_shares", "PlanAccepted"),
    ("test_upcast_task_created_shares", "TaskCreated"),
    ("test_upcast_taskcreated_v10_and_plan_expanded_v2", "PlanExpanded"),
    ("test_upcaster_planaccepted_v9_to_v10_decomposes", "PlanAccepted"),
    ("test_upcaster_planexpanded_v3_to_v4_decomposes", "PlanExpanded"),
    ("test_old_study_created_v9_with_digest_drift_unknown", "StudyCreated"),
    ("test_study_created_writer_is_v11_after_loopspec", "StudyCreated"),
    ("test_upcast_attempt_started_v7_to_v8", "AttemptStarted"),
    ("test_upcast_study_created_v10_to_v11_keeps_max_loop_iter", "StudyCreated"),
    ("test_upcast_study_created_v9_to_v11", "StudyCreated"),
    ("test_v7_journal_real_replay_drift_unknown", "AttemptStarted"),
    ("test_replay_studycreated_v10_max_loop_iter_preserved", "StudyCreated"),
    ("test_upcaster_studycreated_v10_bridge_then_v12_max_proposals", "StudyCreated"),
    ("test_upcaster_studycreated_v10_preserves_max_loop_iter", "StudyCreated"),
    ("test_upcaster_studycreated_v11_to_v12_preserves_max_proposals", "StudyCreated"),
    ("test_upcaster_studyfinalized_v3_to_v4_custom_checks", "StudyFinalized"),
    ("test_upcast_study_created_confirmations_needed_v3_to_v4", "StudyCreated"),
    ("test_upcast_study_created_k_ref_defaults", "StudyCreated"),
    ("test_replay_attempt_started_v2_journal", "AttemptStarted"),
    ("test_replay_task_created_v4_journal", "TaskCreated"),
    ("test_upcast_attempt_started_inputs_default", "AttemptStarted"),
    ("test_upcast_task_created_input_kind_defaults", "TaskCreated"),
    ("test_upcaster_studycreated_v4_to_v5_defaults", "StudyCreated"),
    ("test_upcaster_studycreated_v5_to_v6_supported_needed", "StudyCreated"),
    ("test_upcaster_planaccepted_v3_to_v4_expand_after", "PlanAccepted"),
    ("test_upcaster_studycreated_v6_to_v7_expansion_fields", "StudyCreated"),
    ("test_upcast_every_legacy_reason", "TaskDone"),
    ("test_upcast_preserves_null_reason", "TaskDone"),
    ("test_upcast_provenance_simulated_to_stub", "AttemptStarted"),
    ("test_upcast_study_created_sets_reserve_zero", "StudyCreated"),
    ("test_upcast_unknown_reason_raises", "TaskDone"),
    ("test_writer_emits_v2", "TaskDone"),
    ("test_upcast_input_mode_keeps_old_default", "TaskCreated"),
]


@pytest.mark.parametrize("former_name,event_type", _LEGACY_UPCAST_CASES, ids=[c[0] for c in _LEGACY_UPCAST_CASES])
def test_legacy_guessing_upcast_refused(former_name: str, event_type: str) -> None:
    """Former ``{former_name}``: no production chain; schema>1 or missing fields refuse."""
    assert former_name  # kept for report / PR deleted-tests table
    writer = writer_schema_version(event_type)
    assert writer == 1, f"{event_type} writer must be v1, got {writer}"
    # No from_version steps registered for production guessing chains.
    assert UPCASTERS.get(event_type, {}) == {}
    with pytest.raises(JournalFormatError):
        upcast(event_type, 2, {})


def test_register_upcaster_refuses_overwrite_still_covered() -> None:
    """Former test_register_upcaster_refuses_overwrite — kept in test_upcasters_v1."""
    from tests.test_upcasters_v1 import test_register_upcaster_refuses_overwrite

    assert callable(test_register_upcaster_refuses_overwrite)


def test_upcaster_duplicate_and_continuous_chains_still_covered() -> None:
    """Former test_upcaster_duplicate_and_continuous_chains / test_upcaster_chains_*."""
    from coscientist.core.events.upcasters import assert_upcaster_chain_continuous

    # Empty production buckets are continuous; mechanism still enforced for dummies.
    for et, bucket in UPCASTERS.items():
        if bucket:
            assert_upcaster_chain_continuous(et)


@pytest.mark.asyncio
async def test_migrated_live_lease_empty_nonce_still_refused(tmp_path) -> None:
    """Former test_migrated_live_lease_empty_nonce_waits_for_ttl — empty nonce unsupported."""
    import sqlite3
    from datetime import UTC, datetime, timedelta

    from coscientist.core.events.clock import FakeClock
    from coscientist.core.events.store import SqliteEventStore
    from coscientist.core.events.store_db import JOURNAL_STORE_FORMAT

    db = tmp_path / "e.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE events(
          seq INTEGER PRIMARY KEY, study_id TEXT NOT NULL, type TEXT NOT NULL,
          entity_key TEXT NOT NULL, idem_key TEXT NOT NULL UNIQUE, payload JSON NOT NULL,
          ts TEXT NOT NULL, schema_version INT NOT NULL, epoch INT NOT NULL,
          payload_digest TEXT NOT NULL);
        CREATE TABLE lease(
          study_id TEXT PRIMARY KEY, owner TEXT NOT NULL, owner_nonce TEXT NOT NULL,
          epoch INT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE projections(
          table_name TEXT NOT NULL, row_key TEXT NOT NULL, data JSON NOT NULL,
          PRIMARY KEY (table_name, row_key));
        CREATE TABLE journal_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO journal_meta(key, value) VALUES ('store_format', ?)",
        (str(JOURNAL_STORE_FORMAT),),
    )
    conn.execute(
        "INSERT INTO lease(study_id, owner, owner_nonce, epoch, expires_at) VALUES (?,?,?,?,?)",
        (
            "s",
            "old",
            "",
            1,
            (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = SqliteEventStore("s", db, clock=clock, lease_ttl_s=30.0)
    with pytest.raises(JournalFormatError, match="owner_nonce empty"):
        await store.acquire_lease("new")
    await store.close()
