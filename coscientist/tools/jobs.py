"""JobStore contract + in-memory fake (DESIGN §6.4 / CAPABILITIES #11).

Production wiring through ``StudyWriter`` is a follow-up (coder/alembic backends).
This module owns the Protocol, ``Job`` model, and a journaled in-memory store
for contract tests (R4 foundation). Does not touch ``core/ports.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from coscientist.core.events.clock import Clock
from coscientist.core.events.digest import sha256_hex, spec_digest
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.job_events import (
    JobCancelFailedPayload,
    JobCancelledPayload,
    JobKind,
    JobStatus,
    JobStatusChangedPayload,
    JobSubmittedPayload,
    JobTimedOutPayload,
    deadline_to_wire,
    job_cancel_refused,
    job_is_live,
    job_is_terminal,
)
from coscientist.core.events.schema_versions import JOB_EVENTS_SCHEMA_VERSION
from coscientist.tools.contract import ToolError

JobEventType = Literal[
    "JobSubmitted",
    "JobStatusChanged",
    "JobTimedOut",
    "JobCancelled",
    "JobCancelFailed",
]


class Job(BaseModel):
    """DESIGN §6.4 Job — frozen; no status/artifact inventing."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    job_id: str = Field(min_length=1)
    kind: JobKind
    attempt_id: str = Field(min_length=1)
    status: JobStatus
    deadline: datetime
    artifacts: list[str]

    @field_validator("deadline")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")
        return v


class JobJournal(Protocol):
    """Sync sink — production adapter enqueues onto ``StudyWriter``."""

    def record(self, event: UnstampedEvent) -> None: ...


class JobStore(Protocol):
    def submit(
        self,
        kind: JobKind,
        spec: dict[str, Any],
        attempt_id: str,
        deadline: datetime,
    ) -> str: ...

    def get(self, job_id: str) -> Job: ...

    async def wait(self, job_id: str, until: datetime) -> Job: ...

    def cancel(self, job_id: str) -> None: ...


def _job_id(attempt_id: str, digest: str) -> str:
    return "job_" + sha256_hex(f"{attempt_id}:{digest}")[:16]


class InMemoryJobStore:
    """Journaled fake JobStore for §6.4 contract tests."""

    def __init__(
        self,
        *,
        study_id: str,
        clock: Clock,
        journal: JobJournal,
    ) -> None:
        if not study_id or study_id.strip() != study_id:
            raise ToolError("local", "study_id must be non-empty without whitespace pad")
        self._study_id = study_id
        self._clock = clock
        self._journal = journal
        self._jobs: dict[str, Job] = {}
        self._digests: dict[str, str] = {}
        self._by_attempt: dict[str, str] = {}
        self._cancel_done: set[str] = set()
        self._waiters: dict[str, asyncio.Event] = {}
        # FakeClock.advance/set → tick wakes waiters (DESIGN §6.4 wait ≤ deadline).
        on_change = getattr(clock, "on_change", None)
        if callable(on_change):
            on_change(self.tick)

    def submit(
        self,
        kind: JobKind,
        spec: dict[str, Any],
        attempt_id: str,
        deadline: datetime,
    ) -> str:
        if deadline.tzinfo is None:
            raise ToolError("local", "deadline must be timezone-aware")
        if not attempt_id or attempt_id.strip() != attempt_id:
            raise ToolError("local", "attempt_id must be non-empty without whitespace pad")
        if not isinstance(spec, dict):
            raise ToolError("local", f"spec must be dict, got {type(spec).__name__}")
        digest = spec_digest(spec)
        jid = _job_id(attempt_id, digest)
        existing_id = self._by_attempt.get(attempt_id)
        if existing_id is not None:
            existing = self._jobs[existing_id]
            same = existing_id == jid and self._digests[existing_id] == digest
            if same:
                return existing_id
            if job_is_live(existing.status):
                raise ToolError(
                    "local",
                    f"live job already exists for attempt_id={attempt_id!r}",
                )
        job = Job(
            job_id=jid,
            kind=kind,
            attempt_id=attempt_id,
            status="queued",
            deadline=deadline,
            artifacts=[],
        )
        self._jobs[jid] = job
        self._digests[jid] = digest
        self._by_attempt[attempt_id] = jid
        self._emit(
            "JobSubmitted",
            jid,
            JobSubmittedPayload(
                job_id=jid,
                kind=kind,
                attempt_id=attempt_id,
                deadline=deadline_to_wire(deadline),
                spec_digest=digest,
            ).model_dump(mode="json"),
            make_idem("JobSubmitted", jid, digest),
        )
        return jid

    def get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise ToolError("local", f"unknown job_id={job_id!r}") from None

    def _wait_progress(self, job_id: str, until: datetime) -> Job | None:
        """Apply due timeouts and return if wait should finish; no waiter wake-all."""
        self._apply_due_timeouts()
        job = self.get(job_id)
        if job_is_terminal(job.status):
            return job
        now = self._clock.now()
        if now >= job.deadline:
            return self._apply_timeout(job_id)
        if now >= until:
            return job
        return None

    async def wait(self, job_id: str, until: datetime) -> Job:
        if until.tzinfo is None:
            raise ToolError("local", "until must be timezone-aware")
        while True:
            done = self._wait_progress(job_id, until)
            if done is not None:
                return done
            job = self.get(job_id)
            limit = min(until, job.deadline)
            remaining = (limit - self._clock.now()).total_seconds()
            if remaining <= 0:
                continue
            waiter = self._waiters.get(job_id)
            if waiter is None:
                waiter = asyncio.Event()
                self._waiters[job_id] = waiter
            # FakeClock: on_change→tick wakes. SystemClock: bound by remaining wall.
            if callable(getattr(self._clock, "on_change", None)):
                await waiter.wait()
            else:
                wake = asyncio.create_task(waiter.wait())
                _done, pending = await asyncio.wait({wake}, timeout=remaining)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            waiter.clear()

    def cancel(self, job_id: str) -> None:
        job = self.get(job_id)
        if job_id in self._cancel_done:
            self._emit(
                "JobCancelFailed",
                job_id,
                JobCancelFailedPayload(
                    job_id=job_id,
                    attempt_id=job.attempt_id,
                    reason="cancel_already_recorded",
                ).model_dump(mode="json"),
                make_idem("JobCancelFailed", job_id, "cancel_already_recorded"),
            )
            return
        if job_cancel_refused(job.status):
            self._emit(
                "JobCancelFailed",
                job_id,
                JobCancelFailedPayload(
                    job_id=job_id,
                    attempt_id=job.attempt_id,
                    reason=f"status={job.status}",
                ).model_dump(mode="json"),
                make_idem("JobCancelFailed", job_id, job.status),
            )
            return
        if job_is_live(job.status):
            self._set_status(job_id, "cancelled")
        self._cancel_done.add(job_id)
        self._emit(
            "JobCancelled",
            job_id,
            JobCancelledPayload(job_id=job_id, attempt_id=job.attempt_id).model_dump(mode="json"),
            make_idem("JobCancelled", job_id),
        )
        self._wake(job_id)

    def _apply_due_timeouts(self) -> None:
        now = self._clock.now()
        overdue = [jid for jid, job in list(self._jobs.items()) if job_is_live(job.status) and now >= job.deadline]
        for jid in overdue:
            self._apply_timeout(jid)

    def tick(self) -> None:
        """Close overdue jobs and wake waiters (study tick / FakeClock.on_change)."""
        self._apply_due_timeouts()
        for jid in list(self._waiters):
            self._wake(jid)

    def mark_running(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status != "queued":
            raise ToolError("local", f"mark_running requires queued, got {job.status}")
        return self._set_status(job_id, "running")

    def complete(
        self,
        job_id: str,
        *,
        status: Literal["succeeded", "failed"],
        artifacts: list[str],
    ) -> Job:
        job = self.get(job_id)
        if not job_is_live(job.status):
            raise ToolError("local", f"complete requires live job, got {job.status}")
        updated = job.model_copy(update={"status": status, "artifacts": list(artifacts)})
        self._jobs[job_id] = updated
        self._emit(
            "JobStatusChanged",
            job_id,
            JobStatusChangedPayload(job_id=job_id, attempt_id=job.attempt_id, status=status).model_dump(mode="json"),
            make_idem("JobStatusChanged", job_id, status),
        )
        self._wake(job_id)
        return updated

    def _apply_timeout(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status == "timed_out":
            return job
        if not job_is_live(job.status):
            return job
        self._set_status(job_id, "timed_out")
        self._emit(
            "JobTimedOut",
            job_id,
            JobTimedOutPayload(
                job_id=job_id,
                attempt_id=job.attempt_id,
                deadline=deadline_to_wire(job.deadline),
            ).model_dump(mode="json"),
            make_idem("JobTimedOut", job_id),
        )
        self.cancel(job_id)
        return self.get(job_id)

    def _set_status(self, job_id: str, status: JobStatus) -> Job:
        job = self.get(job_id)
        if job.status == status:
            return job
        if job_is_terminal(job.status):
            raise ToolError(
                "local",
                f"refuse status {job.status!r} → {status!r}",
            )
        updated = job.model_copy(update={"status": status})
        self._jobs[job_id] = updated
        # timed_out → JobTimedOut; cancelled → JobCancelled; else StatusChanged
        if status not in ("timed_out", "cancelled"):
            self._emit(
                "JobStatusChanged",
                job_id,
                JobStatusChangedPayload(job_id=job_id, attempt_id=job.attempt_id, status=status).model_dump(
                    mode="json"
                ),
                make_idem("JobStatusChanged", job_id, status),
            )
        self._wake(job_id)
        return updated

    def _emit(
        self,
        event_type: JobEventType,
        entity_key: str,
        payload: dict[str, Any],
        idem_key: str,
    ) -> None:
        self._journal.record(
            UnstampedEvent(
                study_id=self._study_id,
                type=event_type,
                entity_key=entity_key,
                idem_key=idem_key,
                payload=payload,
                ts=self._clock.now(),
                schema_version=JOB_EVENTS_SCHEMA_VERSION,
            )
        )

    def _wake(self, job_id: str) -> None:
        ev = self._waiters.get(job_id)
        if ev is not None:
            ev.set()
