"""G6 JobStore contract tests (DESIGN §6.4)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from pydantic import ValidationError

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.events import EventType, UnstampedEvent
from coscientist.core.events.job_events import (
    JobCancelFailedPayload,
    JobSubmittedPayload,
    JobTimedOutPayload,
)
from coscientist.tools.contract import ToolError
from coscientist.tools.jobs import InMemoryJobStore, Job


class _Journal:
    def __init__(self) -> None:
        self.events: list[UnstampedEvent] = []

    def record(self, event: UnstampedEvent) -> None:
        self.events.append(event)


def _store(clock: FakeClock | None = None) -> tuple[FakeClock, _Journal, InMemoryJobStore]:
    clock = clock or FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    journal = _Journal()
    store = InMemoryJobStore(study_id="st1", clock=clock, journal=journal)
    return clock, journal, store


def _types(journal: _Journal) -> list[str]:
    return [e.type for e in journal.events]


def test_job_event_types_are_event_type_members() -> None:
    known = set(get_args(EventType))
    for name in (
        "JobSubmitted",
        "JobStatusChanged",
        "JobTimedOut",
        "JobCancelled",
        "JobCancelFailed",
    ):
        assert name in known


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


def test_submit_journals_and_idempotent() -> None:
    _clock, journal, store = _store()
    deadline = _clock.now() + timedelta(seconds=30)
    spec = {"code": "print(1)", "lang": "python"}
    jid = store.submit("coder", spec, "att#1", deadline)
    assert jid.startswith("job_")
    assert store.get(jid).status == "queued"
    assert _types(journal) == ["JobSubmitted"]
    payload = JobSubmittedPayload.model_validate(journal.events[0].payload)
    assert payload.job_id == jid
    assert payload.kind == "coder"
    assert payload.attempt_id == "att#1"
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
    assert timed.deadline == deadline.isoformat().replace("+00:00", "Z")


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
    jid = store.submit(
        "coder", {"x": 1}, "att#5", clock.now() + timedelta(seconds=30)
    )
    store.cancel(jid)
    assert store.get(jid).status == "cancelled"
    assert _types(journal) == ["JobSubmitted", "JobCancelled"]
    store.cancel(jid)
    assert _types(journal)[-1] == "JobCancelFailed"
    failed = JobCancelFailedPayload.model_validate(journal.events[-1].payload)
    assert failed.reason == "cancel_already_recorded"


def test_tick_closes_overdue() -> None:
    clock, journal, store = _store()
    jid = store.submit(
        "coder", {"x": 1}, "att#6", clock.now() + timedelta(seconds=2)
    )
    clock.advance(timedelta(seconds=3))
    store.tick()
    assert store.get(jid).status == "timed_out"
    assert _types(journal) == ["JobSubmitted", "JobTimedOut", "JobCancelled"]
