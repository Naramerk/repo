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
    first = await store.append(ev)
    assert first.seq == 1
    with pytest.raises(Idempotent) as ei:
        await store.append(ev)
    assert ei.value.seq == 1
    store.close()


@pytest.mark.asyncio
async def test_same_owner_reacquire_keeps_epoch(tmp_path: Path, clock: FakeClock):
    store = SqliteEventStore("st1", tmp_path / "e.db", clock=clock)
    epoch1 = await store.acquire_lease("o1", ttl_s=30)
    assert epoch1 == 1
    epoch2 = await store.acquire_lease("o1", ttl_s=30)
    assert epoch2 == 1
    store.close()


@pytest.mark.asyncio
async def test_writer_idempotent_append_returns_existing(
    tmp_path: Path, clock: FakeClock
):
    from coscientist.core.writer import StudyWriter

    store = SqliteEventStore("st1", tmp_path / "e.db", clock=clock)
    await store.acquire_lease("o1")
    writer = StudyWriter(store)
    writer.start()
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
    a = await writer.append(ev)
    b = await writer.append(ev)
    assert a.seq == b.seq == 1
    await writer.stop()
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
async def test_replay_equals_projection(
    tmp_path: Path, clock: FakeClock, runtime_cls: type
):
    from tests.helpers import attach_executor, exp_spec, record_success

    rt = await open_runtime(
        tmp_path, "st_replay", owner="o", clock=clock, runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_replay",
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        aid = await rt.start_attempt(key)
        await record_success(rt, aid, arts)
    await rt.accept_plan([exp_spec("EXP-1")])
    aid = await rt.start_attempt("EXP-1")
    await record_success(rt, aid, arts)
    await rt.cancel_task("REPORT", reason="budget")

    folded = fold_events(rt.store.read_all())
    for table, rows in folded.items():
        proj = {_key(table, r): r for r in rt.store.get_projection(table)}
        for key, data in rows.items():
            assert key in proj, f"missing {table}/{key}"
            for k, v in data.items():
                assert proj[key].get(k) == v or str(proj[key].get(k)) == str(v)

    await close_runtime(rt)


def _key(table: str, row: dict) -> str:
    # Prefer the entity's own id (attempt_id before task_id — attempts embed task_id).
    for f in (
        "result_id",
        "attempt_id",
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
