"""Task state machine + study runtime shell (DESIGN §3.3).

Attempt lifecycle lives in ``attempts``; local fold in ``local_project``;
create/accept_plan in ``lifecycle``. HITL / costly / fork / report are
``coscientist.experimental`` (pre-M2 / pre-M5), not part of the M0 core API.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from coscientist.core.attempts import AttemptsMixin
from coscientist.core.clock import Clock, SystemClock
from coscientist.core.domain import Provenance, Study, Task, TaskOutcome, TaskState
from coscientist.core.errors import LeaseLost
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.graph import GraphStore, KindRegistry, default_execution_registry
from coscientist.core.lifecycle import LifecycleMixin
from coscientist.core.local_project import LocalProjectMixin
from coscientist.core.store import SqliteEventStore
from coscientist.core.study_view import study_view
from coscientist.core.writer import StudyWriter


class StudyRuntime(LifecycleMixin, LocalProjectMixin, AttemptsMixin):
    """In-process owner of one study: store, writer, tasks, timers (M0 state-core)."""

    def __init__(
        self,
        store: SqliteEventStore,
        writer: StudyWriter,
        *,
        clock: Clock | None = None,
        registry: KindRegistry | None = None,
        max_attempts: int = 3,
        owner: str,
    ) -> None:
        if not owner:
            raise ValueError("lease owner is required")
        self.store = store
        self.writer = writer
        self.clock = clock or SystemClock()
        self.registry = registry or default_execution_registry()
        self.graph = GraphStore(self.registry)
        self.max_attempts = max_attempts
        self.owner = owner
        self.study: Study | None = None
        self.tasks: dict[str, Task] = {}
        self.attempts: dict[str, dict[str, Any]] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self._attempt_tasks: dict[str, asyncio.Task[Any]] = {}
        self._lease_lost = False
        self.executor: Any | None = None
        self.dep_outputs: dict[str, dict[str, str]] = {}
        self.dep_provenance: dict[str, Provenance] = {}
        # task_id → retry_at (from AttemptClosed); armed by fold / fail, fired by tick.
        self._retry_at: dict[str, datetime] = {}

    async def _append(self, events: list[EventEnvelope]) -> None:
        if self._lease_lost:
            raise LeaseLost("writer stopped after lease loss")
        try:
            await self.writer.append_many(events)
        except LeaseLost:
            self._lease_lost = True
            raise
        for ev in events:
            self._project_local(ev)

    def view(self):
        sim = sum(
            1
            for t in self.tasks.values()
            if t.state == TaskState.DONE
            and any(
                self.attempts.get(a, {}).get("provenance", "real") != "real"
                for a in t.attempts
            )
        )
        return study_view(self.tasks.values(), open_hitl=0, simulated_done=sim)

    def invariant_open_attempts(self) -> bool:
        """≤1 open attempt per task (DESIGN §3.3 property invariant)."""
        for t in self.tasks.values():
            open_n = 0
            for a in t.attempts:
                if not self.attempts.get(a, {}).get("closed"):
                    open_n += 1
            if open_n > 1:
                return False
        return True

    def invariant_deadlines(self) -> bool:
        """Every task/attempt deadline ≤ study.deadline."""
        if self.study is None:
            return True
        sd = self.study.deadline
        for t in self.tasks.values():
            if t.deadline > sd:
                return False
            for a in t.attempts:
                ad = self.attempts.get(a, {}).get("deadline")
                if ad is None:
                    continue
                if isinstance(ad, str):
                    ad = datetime.fromisoformat(ad)
                if ad > sd:
                    return False
        return True

    async def ensure_lease(self, ttl_s: float = 30.0) -> None:
        if self._lease_lost:
            raise LeaseLost("lease previously lost")
        try:
            await self.store.renew_lease(self.owner, self.store.epoch, ttl_s=ttl_s)
        except LeaseLost:
            self._lease_lost = True
            raise

    async def tick(self) -> None:
        """Apply P5/P6 deadline cancellations and due P3 retries (vs injected Clock).

        HITL timeouts are experimental. No asyncio sleeps/timers for retries —
        ``retry_at`` is journaled and honored after crash/reopen when tick runs.
        """
        if self.study is None or self._lease_lost:
            return
        await self.ensure_lease()
        now = self.clock.now()
        sid = self.study.study_id
        for task in list(self.tasks.values()):
            if task.state == TaskState.DONE:
                continue
            if now > task.deadline or now > self.study.deadline:
                reason = "deadline" if now > task.deadline else "study_deadline"
                outcome = (
                    TaskOutcome.FAILED if reason == "deadline" else TaskOutcome.SKIPPED
                )
                events: list[EventEnvelope] = []
                for aid in task.attempts:
                    if not self.attempts.get(aid, {}).get("closed"):
                        events.append(
                            EventEnvelope(
                                study_id=sid,
                                type="AttemptClosed",
                                entity_key=aid,
                                idem_key=make_idem("AttemptClosed", aid, reason),
                                payload={
                                    "attempt_id": aid,
                                    "closed": True,
                                    "close_reason": reason,
                                    "task_id": task.task_id,
                                },
                                ts=now,
                            )
                        )
                        t = self._attempt_tasks.pop(aid, None)
                        if t:
                            t.cancel()
                events.append(
                    EventEnvelope(
                        study_id=sid,
                        type="TaskDone",
                        entity_key=task.task_id,
                        idem_key=make_idem("TaskDone", task.task_id),
                        payload={
                            "task_id": task.task_id,
                            "outcome": outcome.value,
                            "reason": reason,
                        },
                        ts=now,
                    )
                )
                await self._append(events)

        await self._fire_due_retries(now)

    async def _fire_due_retries(self, now: datetime) -> None:
        """Start attempts whose persisted ``retry_at`` is due (P3).

        Clears ``_retry_at[tid]`` when firing so a retry runs at most once per
        scheduled ``retry_at`` (even if ``tick`` is called again before
        ``AttemptStarted`` is projected).
        """
        if self._lease_lost:
            return
        for tid, retry_at in list(self._retry_at.items()):
            if self._lease_lost:
                return
            if now < retry_at:
                continue
            task = self.tasks.get(tid)
            if task is None or task.state == TaskState.DONE:
                self._retry_at.pop(tid, None)
                continue
            if now > task.deadline or (
                self.study is not None and now > self.study.deadline
            ):
                self._retry_at.pop(tid, None)
                continue
            open_atts = [
                a for a in task.attempts if not self.attempts.get(a, {}).get("closed")
            ]
            if open_atts:
                self._retry_at.pop(tid, None)
                continue
            prov = self._provenance_for_retry(tid)
            self._retry_at.pop(tid, None)
            await self.start_attempt(tid, provenance=prov)


def new_owner() -> str:
    return str(uuid.uuid4())
