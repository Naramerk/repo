"""Attempt start / fail / cancel / retry (extracted to keep machine.py ≤ 500)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from coscientist.core.domain import Provenance, Task, TaskOutcome, TaskState
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.ids import attempt_id, task_id
from coscientist.core.inputs import InputUnavailable, bind_inputs

RETRY_BACKOFF_S = 0.05


class AttemptsMixin:
    """P2/P3/P6 attempt lifecycle methods for StudyRuntime."""

    study: Any
    tasks: dict[str, Task]
    attempts: dict[str, dict[str, Any]]
    clock: Any
    max_attempts: int
    dep_outputs: dict[str, dict[str, str]]
    dep_provenance: dict[str, Provenance]
    _attempt_tasks: dict[str, asyncio.Task[Any]]
    _retry_tasks: dict[str, asyncio.Task[Any]]
    _lease_lost: bool
    _retry_at: dict[str, Any]

    async def _append(self, events: list[EventEnvelope]) -> None:
        raise NotImplementedError

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

    def _dep_map(self) -> dict[str, Task]:
        return {t.spec.task_key: t for t in self.tasks.values()}

    def _resolve_task(self, key_or_id: str) -> Task:
        if key_or_id in self.tasks:
            return self.tasks[key_or_id]
        matches = [t for t in self.tasks.values() if t.spec.task_key == key_or_id]
        if not matches:
            raise KeyError(key_or_id)
        return matches[0]

    async def start_attempt(
        self,
        task_key_or_id: str,
        *,
        provenance: Provenance | None = None,
    ) -> str:
        task = self._resolve_task(task_key_or_id)
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

        try:
            _bindings, derived = bind_inputs(
                task,
                dep_tasks=self._dep_map(),
                dep_outputs=self.dep_outputs,
                dep_provenance=self.dep_provenance,
            )
        except InputUnavailable:
            await self.cancel_task(task.task_id, reason="input_unavailable")
            raise

        prov: Provenance = provenance if provenance is not None else derived
        if prov not in ("real", "partial", "simulated"):
            raise ValueError(f"invalid provenance: {prov}")

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
            "provenance": prov,
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
        return aid

    async def fail_attempt(
        self, aid: str, *, retryable: bool = True, close_reason: str = "failed"
    ) -> None:
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
                idem_key=make_idem("AttemptClosed", aid, close_reason),
                payload={
                    "attempt_id": aid,
                    "closed": True,
                    "close_reason": close_reason,
                    "retryable": retryable,
                    "task_id": tid,
                },
                ts=now,
            )
        ]
        n = int(att["n"])
        can_retry = retryable and n < self.max_attempts and now < task.deadline
        if not can_retry:
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
        else:
            # P3: leave PENDING with retry_at — never RUNNING without an open attempt.
            events[0] = events[0].model_copy(
                update={
                    "payload": {
                        **events[0].payload,
                        "retry_at": (now + timedelta(seconds=RETRY_BACKOFF_S)).isoformat(),
                    }
                }
            )
        await self._append(events)
        if can_retry:
            self._schedule_retry(tid, str(att.get("provenance", "real")))

    def _schedule_retry(self, tid: str, provenance: str) -> None:
        async def _retry() -> None:
            await asyncio.sleep(RETRY_BACKOFF_S)
            if self._lease_lost:
                return
            task = self.tasks.get(tid)
            if task is None or task.state == TaskState.DONE:
                return
            open_atts = [
                a for a in task.attempts if not self.attempts.get(a, {}).get("closed")
            ]
            if open_atts:
                return
            await self.start_attempt(tid, provenance=provenance)  # type: ignore[arg-type]

        old = self._retry_tasks.pop(tid, None)
        if old:
            old.cancel()
        self._retry_tasks[tid] = asyncio.create_task(_retry())

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
