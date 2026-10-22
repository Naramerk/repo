"""Task state machine + study runtime (DESIGN §3.3)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from coscientist.core.clock import Clock, SystemClock
from coscientist.core.domain import HitlRequest, Study, Task, TaskOutcome, TaskState
from coscientist.core.errors import Idempotent, LeaseLost
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.graph import GraphNode, GraphStore, KindRegistry, default_execution_registry
from coscientist.core.ids import attempt_id, task_id
from coscientist.core.lifecycle import LifecycleMixin
from coscientist.core.runtime_ext import HitlForkMixin
from coscientist.core.store import SqliteEventStore
from coscientist.core.study_view import study_view
from coscientist.core.writer import StudyWriter


class StudyRuntime(LifecycleMixin, HitlForkMixin):
    """In-process owner of one study: store, writer, tasks, timers."""

    def __init__(
        self,
        store: SqliteEventStore,
        writer: StudyWriter,
        *,
        clock: Clock | None = None,
        registry: KindRegistry | None = None,
        max_attempts: int = 3,
        owner: str = "runtime",
    ) -> None:
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
        self.hitl: dict[str, HitlRequest] = {}
        self._attempt_tasks: dict[str, asyncio.Task[Any]] = {}
        self._lease_lost = False

    async def _append(self, events: list[EventEnvelope]) -> None:
        if self._lease_lost:
            raise LeaseLost("writer stopped after lease loss")
        try:
            await self.writer.append_many(events)
        except LeaseLost:
            self._lease_lost = True
            raise
        except Idempotent:
            return
        for ev in events:
            self._project_local(ev)

    def _project_local(self, ev: EventEnvelope) -> None:
        p = ev.payload
        if ev.type == "StudyCreated":
            self.study = Study.model_validate(p)
        elif ev.type in ("TaskCreated", "TaskInherited"):
            self.tasks[p["task_id"]] = Task.model_validate(p)
        elif ev.type == "AttemptStarted":
            self.attempts[p["attempt_id"]] = dict(p)
            t = self.tasks[p["task_id"]]
            attempts = list(t.attempts) + [p["attempt_id"]]
            self.tasks[p["task_id"]] = t.model_copy(
                update={"state": TaskState.RUNNING, "attempts": attempts}
            )
        elif ev.type == "AttemptClosed":
            att = self.attempts.get(p["attempt_id"], {})
            att.update(p)
            att["closed"] = True
            self.attempts[p["attempt_id"]] = att
        elif ev.type == "TaskDone":
            t = self.tasks[p["task_id"]]
            self.tasks[p["task_id"]] = t.model_copy(
                update={
                    "state": TaskState.DONE,
                    "outcome": TaskOutcome(p["outcome"]),
                    "reason": p.get("reason"),
                }
            )
        elif ev.type == "HitlOpened":
            self.hitl[p["request_id"]] = HitlRequest.model_validate(p)
        elif ev.type in ("HitlDecided", "HitlTimedOut"):
            h = self.hitl.get(p["request_id"])
            if h:
                self.hitl[p["request_id"]] = h.model_copy(update={"open": False})
        elif ev.type == "HitlDeadlineExtended":
            h = self.hitl.get(p["request_id"])
            if h and "deadline" in p:
                dl = p["deadline"]
                if isinstance(dl, str):
                    dl = datetime.fromisoformat(dl)
                self.hitl[p["request_id"]] = h.model_copy(update={"deadline": dl})
        elif ev.type == "GraphNodeUpserted":
            self.graph.upsert_node(GraphNode.model_validate(p))

    def view(self):
        open_hitl = sum(1 for h in self.hitl.values() if h.open)
        sim = sum(
            1
            for t in self.tasks.values()
            if t.state == TaskState.DONE
            and any(
                self.attempts.get(a, {}).get("provenance", "real") != "real"
                for a in t.attempts
            )
        )
        return study_view(self.tasks.values(), open_hitl=open_hitl, simulated_done=sim)

    def _deps_done(self, task: Task) -> bool:
        for d in task.spec.depends_on:
            dep_id = task_id(self.study.study_id, d) if self.study else d
            dep = self.tasks.get(dep_id) or self.tasks.get(d)
            if dep is None:
                matches = [t for t in self.tasks.values() if t.spec.task_key == d]
                dep = matches[0] if matches else None
            if dep is None or dep.state != TaskState.DONE:
                return False
        return True

    async def start_attempt(
        self,
        task_key_or_id: str,
        *,
        provenance: str = "real",
        hang: bool = False,
    ) -> str:
        task = self._resolve_task(task_key_or_id)
        if task.state != TaskState.PENDING and not (
            task.state == TaskState.RUNNING
            and task.attempts
            and self.attempts.get(task.attempts[-1], {}).get("closed")
        ):
            if task.state == TaskState.DONE:
                raise RuntimeError("task already DONE")
        if not self._deps_done(task):
            raise RuntimeError("dependencies not DONE")
        open_atts = [
            a for a in task.attempts if not self.attempts.get(a, {}).get("closed")
        ]
        if open_atts:
            raise RuntimeError("already has open attempt")
        n = len(task.attempts) + 1
        if n > self.max_attempts:
            raise RuntimeError("max_attempts exceeded")
        aid = attempt_id(task.task_id, n)
        now = self.clock.now()
        assert self.study is not None
        deadline = min(task.deadline, self.study.deadline)
        payload = {
            "attempt_id": aid,
            "n": n,
            "dispatch_id": f"dispatch:{aid}",
            "route_returned": False,
            "closed": False,
            "close_reason": None,
            "provenance": provenance,
            "agent_output_digest": None,
            "deadline": deadline.isoformat(),
            "task_id": task.task_id,
            "study_id": self.study.study_id,
        }
        await self._append(
            [
                EventEnvelope(
                    study_id=self.study.study_id,
                    type="AttemptStarted",
                    entity_key=aid,
                    idem_key=make_idem("AttemptStarted", aid),
                    payload=payload,
                    ts=now,
                )
            ]
        )
        if hang:

            async def _hang() -> None:
                try:
                    await asyncio.sleep(10_000)
                except asyncio.CancelledError:
                    return

            self._attempt_tasks[aid] = asyncio.create_task(_hang())
        return aid

    def _resolve_task(self, key_or_id: str) -> Task:
        if key_or_id in self.tasks:
            return self.tasks[key_or_id]
        matches = [t for t in self.tasks.values() if t.spec.task_key == key_or_id]
        if not matches:
            raise KeyError(key_or_id)
        return matches[0]

    async def finish_attempt(
        self,
        aid: str,
        *,
        outcome: TaskOutcome = TaskOutcome.SUCCESS,
        reason: str | None = None,
    ) -> None:
        att = self.attempts[aid]
        if att.get("closed"):
            raise RuntimeError("attempt closed")
        now = self.clock.now()
        sid = att["study_id"]
        tid = att["task_id"]
        await self._append(
            [
                EventEnvelope(
                    study_id=sid,
                    type="AttemptClosed",
                    entity_key=aid,
                    idem_key=make_idem("AttemptClosed", aid, "finish"),
                    payload={
                        "attempt_id": aid,
                        "closed": True,
                        "close_reason": "result",
                        "task_id": tid,
                    },
                    ts=now,
                ),
                EventEnvelope(
                    study_id=sid,
                    type="TaskDone",
                    entity_key=tid,
                    idem_key=make_idem("TaskDone", tid),
                    payload={
                        "task_id": tid,
                        "outcome": outcome.value,
                        "reason": reason,
                    },
                    ts=now,
                ),
            ]
        )
        t = self._attempt_tasks.pop(aid, None)
        if t:
            t.cancel()

    async def fail_attempt(self, aid: str, *, retryable: bool = True) -> None:
        att = self.attempts[aid]
        if att.get("closed"):
            raise RuntimeError("attempt closed")
        now = self.clock.now()
        sid = att["study_id"]
        tid = att["task_id"]
        task = self.tasks[tid]
        events = [
            EventEnvelope(
                study_id=sid,
                type="AttemptClosed",
                entity_key=aid,
                idem_key=make_idem("AttemptClosed", aid, "fail"),
                payload={
                    "attempt_id": aid,
                    "closed": True,
                    "close_reason": "failed",
                    "retryable": retryable,
                    "task_id": tid,
                },
                ts=now,
            )
        ]
        n = att["n"]
        if not (retryable and n < self.max_attempts and now < task.deadline):
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="TaskDone",
                    entity_key=tid,
                    idem_key=make_idem("TaskDone", tid),
                    payload={
                        "task_id": tid,
                        "outcome": TaskOutcome.FAILED.value,
                        "reason": "failed",
                    },
                    ts=now,
                )
            )
        await self._append(events)

    async def cancel_task(self, task_key_or_id: str, reason: str = "cancelled") -> None:
        task = self._resolve_task(task_key_or_id)
        if task.state == TaskState.DONE:
            return
        now = self.clock.now()
        assert self.study is not None
        events: list[EventEnvelope] = []
        for aid in task.attempts:
            if not self.attempts.get(aid, {}).get("closed"):
                events.append(
                    EventEnvelope(
                        study_id=self.study.study_id,
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
                study_id=self.study.study_id,
                type="TaskDone",
                entity_key=task.task_id,
                idem_key=make_idem("TaskDone", task.task_id),
                payload={
                    "task_id": task.task_id,
                    "outcome": TaskOutcome.SKIPPED.value,
                    "reason": reason,
                },
                ts=now,
            )
        )
        await self._append(events)

    async def ensure_lease(self, ttl_s: float = 30.0) -> None:
        if self._lease_lost:
            raise LeaseLost("lease previously lost")
        try:
            await self.store.renew_lease(self.owner, self.store.epoch, ttl_s=ttl_s)
        except LeaseLost:
            await self.store.acquire_lease(self.owner, ttl_s=ttl_s)

    async def tick(self) -> None:
        """Fire due deadlines (P5/P6) and HITL timeouts."""
        if self.study is None or self._lease_lost:
            return
        await self.ensure_lease()
        now = self.clock.now()
        sid = self.study.study_id
        for _hid, h in list(self.hitl.items()):
            if h.open and now > h.deadline:
                await self._hitl_timeout(h)
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
