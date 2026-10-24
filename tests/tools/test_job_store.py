"""G6 JobStore contract tests (DESIGN §6.4)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from coscientist.core.events.clock import FakeClock, SystemClock
from coscientist.core.events.events import EventType, UnstampedEvent
from coscientist.core.events.job_events import (
    JOB_ID_DIGEST_HEX_LEN,
    JobCancelFailedPayload,
    JobCancelledPayload,
    JobEventType,
    JobStatusChangedPayload,
    JobSubmittedPayload,
    JobTimedOutPayload,
    deadline_to_wire,
    job_cancel_refused,
    job_is_live,
    job_is_terminal,
    require_nonblank,
)
from coscientist.tools.contract import ToolError
from coscientist.tools.jobs import InMemoryJobStore, Job


class _Journal:
    def __init__(self) -> None:
        self.events: list[UnstampedEvent] = []

    def record(self, event: UnstampedEvent) -> None:
        self.events.append(event)


def _store(
    clock: FakeClock | None = None,
) -> tuple[FakeClock, _Journal, InMemoryJobStore]:
    clock = clock or FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    store = InMemoryJobStore(study_id="st1", clock=clock, journal=journal)
    return clock, journal, store


def _types(journal: _Journal) -> list[str]:
    return [e.type for e in journal.events]


def test_job_event_types_are_event_type_members() -> None:
    known = set(get_args(EventType))
    for name in get_args(JobEventType):
        assert name in known


def test_job_status_helpers() -> None:
    assert job_is_live("queued") and job_is_live("running")
    assert not job_is_live("timed_out")
    assert job_is_terminal("timed_out") and job_is_terminal("succeeded")
    assert job_cancel_refused("succeeded")
    assert job_cancel_refused("failed")
    assert job_cancel_refused("cancelled")
    assert not job_cancel_refused("timed_out")
    assert not job_cancel_refused("running")


def test_job_refuses_naive_deadline() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        Job(
            job_id="job_x",
            kind="coder",
            attempt_id="a#1",
            status="queued",
            deadline=datetime(2026, 10, 24),
            artifacts=[],
        )


def test_deadline_to_wire_naive_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        deadline_to_wire(datetime(2026, 10, 24))


def test_deadline_to_wire_normalizes_non_utc_offset() -> None:
    plus3 = timezone(timedelta(hours=3))
    local = datetime(2026, 10, 24, 3, 0, tzinfo=plus3)
    assert deadline_to_wire(local) == "2026-10-24T00:00:00Z"
    assert deadline_to_wire(datetime(2026, 10, 24, tzinfo=UTC)) == "2026-10-24T00:00:00Z"


def test_require_nonblank_and_payload_validators() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        require_nonblank("  x", field="id")
    with pytest.raises(ValueError, match="non-empty"):
        require_nonblank("", field="id")
    with pytest.raises(ValueError, match="non-empty"):
        require_nonblank("x ", field="id")
    with pytest.raises(ValidationError, match="deadline"):
        JobSubmittedPayload(
            job_id="j",
            attempt_id="a",
            kind="coder",
            deadline=" ",
            spec_digest="d",
        )
    with pytest.raises(ValidationError, match="spec_digest"):
        JobSubmittedPayload(
            job_id="j",
            attempt_id="a",
            kind="coder",
            deadline="2026-10-24T00:00:00Z",
            spec_digest="",
        )
    with pytest.raises(ValidationError, match="deadline"):
        JobTimedOutPayload(job_id="j", attempt_id="a", deadline="\t")
    with pytest.raises(ValidationError, match="reason"):
        JobCancelFailedPayload(job_id="j", attempt_id="a", reason=" ")
    with pytest.raises(ValidationError, match="job_id"):
        JobCancelledPayload(job_id=" j", attempt_id="a")
    JobStatusChangedPayload(job_id="j", attempt_id="a", status="running")


def test_submit_journals_and_idempotent() -> None:
    _clock, journal, store = _store()
    deadline = _clock.now() + timedelta(seconds=30)
    spec = {"code": "print(1)", "lang": "python"}
    jid = store.submit("coder", spec, "att#1", deadline)
    assert jid.startswith("job_")
    assert len(jid) == len("job_") + JOB_ID_DIGEST_HEX_LEN
    assert store.get(jid).status == "queued"
    assert _types(journal) == ["JobSubmitted"]
    payload = JobSubmittedPayload.model_validate(journal.events[0].payload)
    assert payload.job_id == jid
    assert payload.kind == "coder"
    assert payload.attempt_id == "att#1"
    assert payload.deadline == deadline_to_wire(deadline)
    assert store.submit("coder", spec, "att#1", deadline) == jid
    assert _types(journal) == ["JobSubmitted"]


def test_second_live_job_same_attempt_refused() -> None:
    clock, _journal, store = _store()
    deadline = clock.now() + timedelta(seconds=30)
    store.submit("coder", {"a": 1}, "att#1", deadline)
    with pytest.raises(ToolError, match="live job") as ei:
        store.submit("coder", {"a": 2}, "att#1", deadline)
    assert ei.value.kind == "local"
    assert ei.value.retryable is False


def test_store_init_and_submit_refuses() -> None:
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    with pytest.raises(ToolError, match="study_id"):
        InMemoryJobStore(study_id="", clock=clock, journal=journal)
    with pytest.raises(ToolError, match="study_id"):
        InMemoryJobStore(study_id=" st", clock=clock, journal=journal)
    store = InMemoryJobStore(study_id="st1", clock=clock, journal=journal)
    aware = clock.now() + timedelta(seconds=1)
    with pytest.raises(ToolError, match="timezone-aware"):
        store.submit("coder", {}, "att", datetime(2026, 10, 24))
    with pytest.raises(ToolError, match="attempt_id"):
        store.submit("coder", {}, "", aware)
    with pytest.raises(ToolError, match="attempt_id"):
        store.submit("coder", {}, " a", aware)
    bad_spec: Any = ["not", "dict"]
    with pytest.raises(ToolError, match="spec must be dict"):
        store.submit("coder", bad_spec, "att", aware)


def test_get_unknown_job_id() -> None:
    _clock, _journal, store = _store()
    with pytest.raises(ToolError, match="unknown job_id"):
        store.get("job_missing")


@pytest.mark.asyncio
async def test_wait_refuses_naive_until() -> None:
    clock, _journal, store = _store()
    jid = store.submit("coder", {"x": 1}, "att#u", clock.now() + timedelta(seconds=30))
    with pytest.raises(ToolError, match="timezone-aware"):
        await store.wait(jid, until=datetime(2026, 10, 24))


def test_complete_and_terminal_transition_refused() -> None:
    clock, _journal, store = _store()
    jid = store.submit("coder", {"x": 1}, "att#c", clock.now() + timedelta(seconds=30))
    store.cancel(jid)
    with pytest.raises(ToolError, match="complete requires live"):
        store.complete(jid, status="succeeded", artifacts=[])
    with pytest.raises(ToolError, match="queued"):
        store.mark_running(jid)
    with pytest.raises(ToolError, match="refuse status"):
        store._set_status(jid, "running")


@pytest.mark.parametrize("terminal", ["succeeded", "failed"], ids=["succeeded", "failed"])
def test_cancel_refused_on_succeeded_or_failed(terminal: str) -> None:
    clock, journal, store = _store()
    jid = store.submit("coder", {"t": terminal}, "att#" + terminal, clock.now() + timedelta(seconds=30))
    store.mark_running(jid)
    if terminal == "succeeded":
        store.complete(jid, status="succeeded", artifacts=[])
    else:
        store.complete(jid, status="failed", artifacts=[])
    store.cancel(jid)
    assert _types(journal)[-1] == "JobCancelFailed"
    failed = JobCancelFailedPayload.model_validate(journal.events[-1].payload)
    assert failed.reason == f"status={terminal}"
    assert store.get(jid).status == terminal


async def _advance_after_yield(clock: FakeClock, delta: timedelta) -> None:
    """Yield so ``wait`` can park, then advance (on_change → tick wakes)."""
    await asyncio.sleep(0)
    clock.advance(delta)


@pytest.mark.asyncio
async def test_wait_returns_when_until_passes_via_clock_advance() -> None:
    """DESIGN §6.4: wait bounded by until; FakeClock.advance must not hang."""
    clock, _journal, store = _store()
    deadline = clock.now() + timedelta(seconds=30)
    jid = store.submit("coder", {"x": 1}, "att#until", deadline)
    until = clock.now() + timedelta(seconds=5)

    adv = asyncio.create_task(_advance_after_yield(clock, timedelta(seconds=10)))
    job = await asyncio.wait_for(store.wait(jid, until=until), timeout=2.0)
    await adv
    assert job.status == "queued"
    assert job.job_id == jid
    assert clock.now() >= until
    assert clock.now() < deadline


@pytest.mark.asyncio
async def test_wait_times_out_when_deadline_passes_via_clock_advance() -> None:
    clock, journal, store = _store()
    deadline = clock.now() + timedelta(seconds=5)
    jid = store.submit("coder", {"x": 1}, "att#dl", deadline)

    adv = asyncio.create_task(_advance_after_yield(clock, timedelta(seconds=6)))
    job = await asyncio.wait_for(
        store.wait(jid, until=clock.now() + timedelta(seconds=60)),
        timeout=2.0,
    )
    await adv
    assert job.status == "timed_out"
    assert "JobTimedOut" in _types(journal)


@pytest.mark.asyncio
async def test_wait_returns_when_until_passes_on_system_clock() -> None:
    """Production path: SystemClock wait ends by wall remaining (single wait path)."""
    clock = SystemClock()
    journal = _Journal()
    store = InMemoryJobStore(study_id="st1", clock=clock, journal=journal)
    deadline = clock.now() + timedelta(seconds=30)
    jid = store.submit("coder", {"x": 1}, "att#sys", deadline)
    until = clock.now() + timedelta(milliseconds=150)
    job = await asyncio.wait_for(store.wait(jid, until=until), timeout=2.0)
    assert job.status == "queued"
    assert job.job_id == jid
    assert clock.now() >= until
    assert clock.now() < deadline


@pytest.mark.asyncio
async def test_deadline_timed_out_then_cancelled() -> None:
    clock, journal, store = _store()
    deadline = clock.now() + timedelta(seconds=5)
    jid = store.submit("alembic", {"step": "build"}, "att#2", deadline)
    clock.advance(timedelta(seconds=6))
    job = await store.wait(jid, until=clock.now() + timedelta(seconds=60))
    assert job.status == "timed_out"
    assert _types(journal) == ["JobSubmitted", "JobTimedOut", "JobCancelled"]
    timed = JobTimedOutPayload.model_validate(journal.events[1].payload)
    assert timed.job_id == jid
    assert timed.deadline == deadline_to_wire(deadline)


@pytest.mark.asyncio
async def test_wait_never_revives_timed_out() -> None:
    clock, _journal, store = _store()
    deadline = clock.now() + timedelta(seconds=1)
    jid = store.submit("coder", {"x": 1}, "att#3", deadline)
    clock.advance(timedelta(seconds=2))
    first = await store.wait(jid, until=clock.now())
    assert first.status == "timed_out"
    second = await store.wait(jid, until=clock.now() + timedelta(hours=1))
    assert second.status == "timed_out"
    with pytest.raises(ToolError, match="queued"):
        store.mark_running(jid)


@pytest.mark.asyncio
async def test_complete_success_wakes_wait() -> None:
    clock, journal, store = _store()
    deadline = clock.now() + timedelta(seconds=60)
    jid = store.submit("coder", {"x": 1}, "att#4", deadline)
    store.mark_running(jid)

    async def _finish() -> None:
        await asyncio.sleep(0)
        store.complete(jid, status="succeeded", artifacts=["art_a"])

    fin = asyncio.create_task(_finish())
    job = await store.wait(jid, until=deadline)
    await fin
    assert job.status == "succeeded"
    assert job.artifacts == ["art_a"]
    assert "JobStatusChanged" in _types(journal)


def test_cancel_live_then_second_cancel_fails() -> None:
    clock, journal, store = _store()
    jid = store.submit("coder", {"x": 1}, "att#5", clock.now() + timedelta(seconds=30))
    store.cancel(jid)
    assert store.get(jid).status == "cancelled"
    assert _types(journal) == ["JobSubmitted", "JobCancelled"]
    store.cancel(jid)
    assert _types(journal)[-1] == "JobCancelFailed"
    failed = JobCancelFailedPayload.model_validate(journal.events[-1].payload)
    assert failed.reason == "cancel_already_recorded"


def test_tick_closes_overdue() -> None:
    clock, journal, store = _store()
    jid = store.submit("coder", {"x": 1}, "att#6", clock.now() + timedelta(seconds=2))
    clock.advance(timedelta(seconds=3))
    store.tick()
    assert store.get(jid).status == "timed_out"
    assert _types(journal) == ["JobSubmitted", "JobTimedOut", "JobCancelled"]
