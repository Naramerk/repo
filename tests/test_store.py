"""EventStore: idempotency, lease fencing, replay == projection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.digest import payload_digest
from coscientist.core.events.errors import Conflict, Idempotent, LeaseLost
from coscientist.core.events.events import EventEnvelope
from coscientist.core.events.projections import fold_events
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.upcasters import writer_schema_version
from tests.helpers import close_runtime, open_runtime


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
        schema_version=writer_schema_version('StudyCreated'),
epoch=0,
)
    first = await store.append(ev)
    assert first.seq == 1
    with pytest.raises(Idempotent) as ei:
        await store.append(ev)
    assert ei.value.seq == 1
    await store.close()


@pytest.mark.asyncio
async def test_same_owner_reacquire_keeps_epoch(tmp_path: Path, clock: FakeClock):
    store = SqliteEventStore("st1", tmp_path / "e.db", clock=clock)
    epoch1 = await store.acquire_lease("o1", ttl_s=30)
    assert epoch1 == 1
    nonce = store.owner_nonce
    assert nonce
    epoch2 = await store.acquire_lease("o1", ttl_s=30)
    assert epoch2 == 1
    assert store.owner_nonce == nonce
    await store.close()


@pytest.mark.asyncio
async def test_store_close_idempotent(tmp_path: Path, clock: FakeClock):
    store = SqliteEventStore("st1", tmp_path / "close.db", clock=clock)
    await store.close()
    await store.close()
    assert store.closed is True


@pytest.mark.asyncio
async def test_writer_idempotent_append_returns_existing(
    tmp_path: Path, clock: FakeClock
):
    from coscientist.core.events.writer import StudyWriter

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
        schema_version=writer_schema_version('StudyCreated'),
epoch=0,
)
    a = await writer.append(ev)
    b = await writer.append(ev)
    assert a.seq == b.seq == 1
    await writer.stop()
    await store.close()


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
        schema_version=writer_schema_version('StudyCreated'),
epoch=0,
)
    await store.append(ev1)
    ev2 = ev1.model_copy(
        update={"payload": {"a": 2}, "payload_digest": payload_digest({"a": 2})}
    )
    with pytest.raises(Conflict):
        await store.append(ev2)
    await store.close()


@pytest.mark.asyncio
async def test_lease_epoch_fencing(tmp_path: Path, clock: FakeClock):
    store = SqliteEventStore("st1", tmp_path / "e.db", clock=clock)
    epoch1 = await store.acquire_lease("o1", ttl_s=30)
    assert epoch1 == 1
    stale_nonce = store.owner_nonce
    assert stale_nonce
    # expire lease
    clock.advance(timedelta(seconds=31))
    epoch2 = await store.acquire_lease("o2", ttl_s=30)
    assert epoch2 == 2
    # Stale holder identity (owner+epoch+nonce) cannot write — seeding owner/epoch
    # alone is not enough once fencing requires owner_nonce.
    store.owner = "o1"
    store.epoch = epoch1
    store.owner_nonce = stale_nonce
    with pytest.raises(LeaseLost):
        await store.append(
            EventEnvelope(
                study_id="st1",
                type="StudyCreated",
                entity_key="st1",
                idem_key="StudyCreated:st1",
                payload={},
                ts=clock.now(),
                schema_version=writer_schema_version('StudyCreated'),
                    epoch=0,
                    payload_digest=payload_digest({}),
                )
        )
    await store.close()


@pytest.mark.asyncio
async def test_seeded_owner_epoch_without_nonce_cannot_write(
    tmp_path: Path, clock: FakeClock
):
    """Constructor owner/epoch without owner_nonce (no acquire_lease) cannot write."""
    store = SqliteEventStore(
        "st1", tmp_path / "seed.db", clock=clock, owner="seeded", epoch=1
    )
    with pytest.raises(LeaseLost, match="no lease owner"):
        await store.append(
            EventEnvelope(
                study_id="st1",
                type="StudyCreated",
                entity_key="st1",
                idem_key="StudyCreated:st1",
                payload={},
                ts=clock.now(),
                schema_version=writer_schema_version('StudyCreated'),
                    epoch=0,
                    payload_digest=payload_digest({}),
                )
        )
    await store.close()


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

    folded = fold_events(await rt.store.read_all())
    for table, rows in folded.items():
        proj = {_key(table, r): r for r in await rt.store.get_projection(table)}
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


def _assert_json_values(obj: object) -> None:
    """Projection/event data must be plain JSON types (no datetime/Decimal/…)."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return
    if isinstance(obj, list):
        for x in obj:
            _assert_json_values(x)
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert isinstance(k, str)
            _assert_json_values(v)
        return
    raise AssertionError(f"non-JSON type in projection: {type(obj).__name__}={obj!r}")


@pytest.mark.asyncio
async def test_upsert_projection_rejects_datetime(tmp_path: Path, clock: FakeClock):
    """Strict JSON: datetime in projection data raises (would have been ISO via _json_default)."""
    store = SqliteEventStore("st_json", tmp_path / "json.db", clock=clock)
    await store.acquire_lease("o1")
    with pytest.raises(TypeError, match="not JSON serializable|datetime"):
        await store.upsert_projection(
            "tasks",
            "t1",
            {
                "task_id": "t1",
                "deadline": datetime(2026, 1, 1, tzinfo=UTC),
            },
        )
    await store.close()


@pytest.mark.asyncio
async def test_attempt_closed_tasks_projection_is_strict_json(
    tmp_path: Path, clock: FakeClock, runtime_cls: type
):
    """AttemptClosed writes tasks (incl. retry_at); all projection values are JSON types."""
    from coscientist.core.execution.attempts import RETRY_BACKOFF_S
    from tests.helpers import attach_executor

    rt = await open_runtime(
        tmp_path, "st_json_p3", clock=clock, runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_json_p3",
    )
    attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await rt.fail_attempt(aid, retryable=True)
    task_row = await rt.store.get_row("tasks", "st_json_p3/SPEC")
    assert task_row is not None
    assert task_row.get("reason") == "retry_scheduled"
    assert isinstance(task_row.get("retry_at"), str)
    _assert_json_values(task_row)
    att_row = await rt.store.get_row("attempts", aid)
    assert att_row is not None
    assert isinstance(att_row.get("retry_at"), str)
    _assert_json_values(att_row)
    # In-txn write path also rejects datetime (no silent stringify).
    with pytest.raises(TypeError, match="not JSON serializable|datetime"):
        rt.store._upsert_projection_write(  # noqa: SLF001
            "tasks",
            "st_json_p3/SPEC",
            {**task_row, "deadline": clock.now()},
        )
    clock.advance(timedelta(seconds=RETRY_BACKOFF_S))
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_event_loop_progresses_during_slow_write(
    tmp_path: Path, clock: FakeClock, monkeypatch
):
    """Write sleeps off-loop; concurrent tick stays schedulable mid-write."""
    import threading

    from coscientist.core.execution import runtime_factory as rf
    from tests.store_doubles import SlowWriteStore

    monkeypatch.setattr(rf, "SqliteEventStore", SlowWriteStore)
    rt = await open_runtime(tmp_path, "st_slow", clock=clock)
    assert isinstance(rt.store, SlowWriteStore)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_slow",
    )
    entered = threading.Event()
    rt.store.write_delay_s = 0.35
    rt.store.write_delay_entered = entered

    peer_saw_in_flight = False

    async def _peer_tick() -> None:
        nonlocal peer_saw_in_flight
        while not entered.is_set():
            await asyncio.sleep(0.01)
        assert not write_task.done(), "write should still be in flight"
        peer_saw_in_flight = True
        await rt.tick()

    write_task = asyncio.create_task(
        rt._append(  # noqa: SLF001
            [
                EventEnvelope(
                    study_id="st_slow",
                    type="GraphNodeUpserted",
                    entity_key="st_slow:probe",
                    idem_key="GraphNodeUpserted:st_slow:probe",
                    payload={
                        "node_id": "st_slow:probe",
                        "kind": "study",
                        "study_id": "st_slow",
                        "ref_id": "st_slow",
                    },
                    ts=clock.now(),
                    schema_version=writer_schema_version('GraphNodeUpserted'),
epoch=0, payload_digest=payload_digest({
                        "node_id": "st_slow:probe",
                        "kind": "study",
                        "study_id": "st_slow",
                        "ref_id": "st_slow",
                    }),
)
            ]
        )
    )
    tick_task = asyncio.create_task(_peer_tick())
    await asyncio.wait_for(asyncio.gather(write_task, tick_task), timeout=3.0)
    assert peer_saw_in_flight
    assert entered.is_set()
    rt.store.write_delay_s = 0.0
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_concurrent_read_during_slow_write_does_not_stall_loop(
    tmp_path: Path, clock: FakeClock, monkeypatch
):
    """WAL read connection: read_all completes while a slow write still holds the write lock."""
    import threading

    from coscientist.core.execution import runtime_factory as rf
    from tests.store_doubles import SlowWriteStore

    monkeypatch.setattr(rf, "SqliteEventStore", SlowWriteStore)
    rt = await open_runtime(tmp_path, "st_rd", clock=clock)
    assert isinstance(rt.store, SlowWriteStore)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_rd",
    )
    entered = threading.Event()
    rt.store.write_delay_s = 0.4
    rt.store.write_delay_entered = entered
    read_finished_during_write = False

    async def _peer_read() -> None:
        nonlocal read_finished_during_write
        while not entered.is_set():
            await asyncio.sleep(0.01)
        assert not write_task.done()
        events = await rt.store.read_all()
        assert isinstance(events, list)
        assert len(events) >= 1
        # Still in flight: read did not wait for the write lock / delay.
        assert not write_task.done()
        read_finished_during_write = True

    write_task = asyncio.create_task(
        rt._append(  # noqa: SLF001
            [
                EventEnvelope(
                    study_id="st_rd",
                    type="GraphNodeUpserted",
                    entity_key="st_rd:probe",
                    idem_key="GraphNodeUpserted:st_rd:probe",
                    payload={
                        "node_id": "st_rd:probe",
                        "kind": "study",
                        "study_id": "st_rd",
                        "ref_id": "st_rd",
                    },
                    ts=clock.now(),
                    schema_version=writer_schema_version('GraphNodeUpserted'),
epoch=0, payload_digest=payload_digest({
                        "node_id": "st_rd:probe",
                        "kind": "study",
                        "study_id": "st_rd",
                        "ref_id": "st_rd",
                    }),
)
            ]
        )
    )
    read_task = asyncio.create_task(_peer_read())
    await asyncio.wait_for(asyncio.gather(write_task, read_task), timeout=3.0)
    assert read_finished_during_write
    rt.store.write_delay_s = 0.0
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_append_rejects_datetime_in_payload(tmp_path: Path, clock: FakeClock):
    """Append with datetime in payload raises at json.dumps (strict; no stringification).

    Pre-sets payload_digest so the failure is the store's JSON encode, not JCS digest.
    """
    store = SqliteEventStore("st_json2", tmp_path / "json2.db", clock=clock)
    await store.acquire_lease("o1")
    with pytest.raises(TypeError, match="not JSON serializable|datetime"):
        await store.append(
            EventEnvelope(
                study_id="st_json2",
                type="StudyCreated",
                entity_key="st_json2",
                idem_key="StudyCreated:st_json2",
                payload={"study_id": "st_json2", "created_at": clock.now()},
                ts=clock.now(),
                # Skip digest so we exercise json.dumps in _insert_one.
                payload_digest=payload_digest({"study_id": "st_json2", "created_at": clock.now()}) * 64,
                schema_version=writer_schema_version('StudyCreated'),
epoch=0,
)
        )
    await store.close()


@pytest.mark.asyncio
async def test_read_connection_rejects_writes(tmp_path: Path, clock: FakeClock):
    """The WAL read connection is opened read-only; writes must fail."""
    import sqlite3

    store = SqliteEventStore("st_ro", tmp_path / "ro.db", clock=clock)
    await store.acquire_lease("o1")
    with pytest.raises(sqlite3.OperationalError, match="readonly|read-only|READ"):
        store._read_conn.execute(  # noqa: SLF001
            "INSERT INTO projections(table_name, row_key, data) VALUES (?,?,?)",
            ("t", "k", "{}"),
        )
    await store.close()


@pytest.mark.asyncio
async def test_concurrent_reads_under_read_lock(tmp_path: Path, clock: FakeClock):
    """Many concurrent read_all/get_projection calls succeed under the read lock."""
    rt = await open_runtime(tmp_path, "st_cread", clock=clock)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_cread",
    )

    async def _one(i: int) -> int:
        events = await rt.store.read_all()
        proj = await rt.store.get_projection("tasks")
        assert events
        assert isinstance(proj, list)
        return i

    results = await asyncio.gather(*[_one(i) for i in range(32)])
    assert results == list(range(32))
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_close_waits_for_in_flight_read(tmp_path: Path, clock: FakeClock):
    """close() takes the read lock after in-flight readers finish."""
    import threading

    store = SqliteEventStore("st_cwait", tmp_path / "cwait.db", clock=clock)
    await store.acquire_lease("o1")
    entered = threading.Event()
    release_read = threading.Event()

    async def hold_read_lock() -> None:
        def _blocked() -> None:
            with store._read_lock:  # noqa: SLF001
                entered.set()
                release_read.wait(timeout=5.0)

        await asyncio.to_thread(_blocked)

    read_task = asyncio.create_task(hold_read_lock())
    assert await asyncio.to_thread(entered.wait, 5.0)
    close_task = asyncio.create_task(store.close())
    await asyncio.sleep(0.05)
    assert not close_task.done(), "close must wait for the in-flight read lock holder"
    release_read.set()
    await asyncio.wait_for(asyncio.gather(read_task, close_task), timeout=3.0)
    assert store.closed
