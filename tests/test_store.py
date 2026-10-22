"""EventStore: idempotency, lease fencing, replay == projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coscientist.core.clock import FakeClock
from coscientist.core.digest import payload_digest
from coscientist.core.errors import Conflict, Idempotent, LeaseLost
from coscientist.core.events import EventEnvelope
from coscientist.core.projections import fold_events
from coscientist.core.runtime_factory import close_runtime, open_runtime
from coscientist.core.store import SqliteEventStore


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.mark.asyncio
async def test_idempotent_same_payload(tmp_path: Path, clock: FakeClock):
    store = SqliteEventStore("st1", tmp_path / "e.db", clock=clock)
    await store.acquire_lease("o1")
    payload = {"study_id": "st1", "x": 1}
    ev = EventEnvelope(
        study_id="st1",
        type="StudyCreated",
        entity_key="st1",
        idem_key="StudyCreated:st1",
        payload=payload,
        ts=clock.now(),
        payload_digest=payload_digest(payload),
    )
    await store.append(ev)
    with pytest.raises(Idempotent):
        await store.append(ev)
    store.close()


@pytest.mark.asyncio
async def test_conflict_different_payload(tmp_path: Path, clock: FakeClock):
    store = SqliteEventStore("st1", tmp_path / "e.db", clock=clock)
    await store.acquire_lease("o1")
    ev1 = EventEnvelope(
        study_id="st1",
        type="StudyCreated",
        entity_key="st1",
        idem_key="StudyCreated:st1",
        payload={"a": 1},
        ts=clock.now(),
        payload_digest=payload_digest({"a": 1}),
    )
    await store.append(ev1)
    ev2 = ev1.model_copy(
        update={"payload": {"a": 2}, "payload_digest": payload_digest({"a": 2})}
    )
    with pytest.raises(Conflict):
        await store.append(ev2)
    store.close()


@pytest.mark.asyncio
async def test_lease_epoch_fencing(tmp_path: Path, clock: FakeClock):
    store = SqliteEventStore("st1", tmp_path / "e.db", clock=clock)
    epoch1 = await store.acquire_lease("o1", ttl_s=30)
    assert epoch1 == 1
    # expire lease
    clock.advance(timedelta(seconds=31))
    epoch2 = await store.acquire_lease("o2", ttl_s=30)
    assert epoch2 == 2
    # stale owner cannot write
    store.owner = "o1"
    store.epoch = epoch1
    with pytest.raises(LeaseLost):
        await store.append(
            EventEnvelope(
                study_id="st1",
                type="StudyCreated",
                entity_key="st1",
                idem_key="StudyCreated:st1",
                payload={},
                ts=clock.now(),
            )
        )
    store.close()


@pytest.mark.asyncio
async def test_replay_equals_projection(tmp_path: Path, clock: FakeClock):
    rt = await open_runtime(tmp_path, "st_replay", owner="o", clock=clock)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_replay",
    )
    # finish stage tasks quickly via cancel/skip remaining after ticking past? finish SPEC..PLAN
    for key in ("SPEC", "HYP", "LIT"):
        aid = await rt.start_attempt(key)
        await rt.finish_attempt(aid)
    from coscientist.core.domain import TaskSpec

    await rt.accept_plan(
        [
            TaskSpec(
                task_key="EXP-1",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
            )
        ]
    )
    aid = await rt.start_attempt("EXP-1")
    await rt.finish_attempt(aid)
    # report
    aid = await rt.start_attempt("REPORT")
    await rt.finish_attempt(aid)

    folded = fold_events(rt.store.read_all())
    for table, rows in folded.items():
        proj = { _key(table, r): r for r in rt.store.get_projection(table) }
        for key, data in rows.items():
            assert key in proj, f"missing {table}/{key}"
            # compare core fields
            for k, v in data.items():
                assert proj[key].get(k) == v or str(proj[key].get(k)) == str(v)

    await close_runtime(rt)


def _key(table: str, row: dict) -> str:
    # Prefer the entity's own id (attempt_id before task_id — attempts embed task_id).
    for f in (
        "attempt_id",
        "result_id",
        "request_id",
        "node_id",
        "edge_id",
        "plan_id",
        "artifact_id",
        "task_id",
        "id",
        "study_id",
    ):
        if f in row:
            return str(row[f])
    return str(row)
