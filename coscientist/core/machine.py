"""Task state machine + study runtime shell (DESIGN §3.3).

Attempt lifecycle lives in ``attempts``; local fold in ``local_project``;
create/accept_plan in ``lifecycle``. Finalizer/cancel in finalize/cancel.
HITL / costly / fork / report are ``coscientist.experimental``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from coscientist.core.attempts import AttemptsMixin
from coscientist.core.cancel import cancel_study as _cancel_study
from coscientist.core.clock import Clock, SystemClock
from coscientist.core.domain import Provenance, Study, Task, TaskOutcome, TaskState
from coscientist.core.errors import LeaseLost
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.finalize import (
    FinalizeTrigger,
    pick_finalize_trigger,
)
from coscientist.core.finalize import (
    finalize as _finalize,
)
from coscientist.core.graph import GraphStore, KindRegistry, default_execution_registry
from coscientist.core.intake import IntakeRefuse
from coscientist.core.lifecycle import LifecycleMixin
from coscientist.core.local_project import LocalProjectMixin
from coscientist.core.reasons import CloseReason, TaskReason
from coscientist.core.store import SqliteEventStore
from coscientist.core.study_view import study_view
from coscientist.core.writer import StudyWriter


class StudyRuntime(LifecycleMixin, LocalProjectMixin, AttemptsMixin):
    """In-process owner of one study: store, writer, tasks, timers."""

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
        self.plans: dict[str, dict[str, Any]] = {}
        self._attempt_tasks: dict[str, asyncio.Task[Any]] = {}
        self._lease_lost = False
        self.executor: Any | None = None
        self.artifacts: Any | None = None
        self.dep_outputs: dict[str, dict[str, str]] = {}
        self.dep_provenance: dict[str, Provenance] = {}
        self._retry_at: dict[str, datetime] = {}
        self._finalized = False
        self._study_cancelled = False
        self._budget_exceeded = False
        self._intake_refusal: IntakeRefuse | None = None
        self._study_spent_rub = 0.0
        self._study_llm_calls = 0

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
        from coscientist.core.dispositions import dispositions

        qdisp = None
        if self.plans:
            qdisp = dispositions(tasks=self.tasks, plans=self.plans)
        ed = self.study.effective_deadline() if self.study else None
        return study_view(
            self.tasks.values(),
            open_hitl=0,
            attempts=self.attempts,
            finalized=self._finalized,
            now=self.clock.now(),
            effective_deadline=ed,
            question_dispositions=qdisp,
        )

    def invariant_open_attempts(self) -> bool:
        for t in self.tasks.values():
            open_n = sum(
                1 for a in t.attempts if not self.attempts.get(a, {}).get("closed")
            )
            if open_n > 1:
                return False
        return True

    def invariant_deadlines(self) -> bool:
        if self.study is None:
            return True
        ed = self.study.effective_deadline()
        for t in self.tasks.values():
            if t.deadline > ed:
                return False
            for a in t.attempts:
                ad = self.attempts.get(a, {}).get("deadline")
                if ad is None:
                    continue
                if isinstance(ad, str):
                    ad = datetime.fromisoformat(ad)
                if ad > ed:
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

    async def cancel_study(self, by: str = "user") -> None:
        await _cancel_study(self, by=by)

    async def note_budget_exceeded(self) -> None:
        """Persist study-level ``BudgetExceeded`` (folded → budget finalize trigger)."""
        if self.study is None or self._budget_exceeded or self._finalized:
            return
        sid = self.study.study_id
        await self._append(
            [
                EventEnvelope(
                    study_id=sid,
                    type="BudgetExceeded",
                    entity_key=sid,
                    idem_key=make_idem("BudgetExceeded", sid, "study"),
                    payload={"scope": "study"},
                    ts=self.clock.now(),
                    schema_version=1,
                )
            ]
        )

    def _study_budget_exhausted(self) -> bool:
        """True when folded spend/calls hit ``Budget.max_cost_rub`` / ``max_llm_calls``."""
        if self.study is None or self.study.budget is None:
            return False
        bud = self.study.budget
        if bud.max_cost_rub is not None and self._study_spent_rub >= float(
            bud.max_cost_rub
        ):
            return True
        if bud.max_llm_calls > 0 and self._study_llm_calls >= bud.max_llm_calls:
            return True
        return False

    async def _emit_study_budget_if_exhausted(self) -> None:
        """Production producer: spend via BudgetDebited → BudgetExceeded(study)."""
        if self._budget_exceeded or self._finalized:
            return
        if self._study_budget_exhausted():
            await self.note_budget_exceeded()

    async def finalize(self, *, trigger: FinalizeTrigger) -> None:
        await _finalize(self, trigger=trigger)

    def _budget_trigger_active(self) -> bool:
        """Study-level budget only (``BudgetExceeded`` / folded flag).

        A single ``TaskDone(..., reason=budget)`` from a costly/HITL route does
        **not** close the study — that is a task-level skip only.
        """
        return bool(self._budget_exceeded)

    async def tick(self) -> None:
        """Fence lease, P5 for deadline<ed, then finalize triggers, retries.

        ``ensure_lease`` runs even after StudyFinalized so stale epochs still
        raise LeaseLost. P5 (task.deadline < effective_deadline) runs before
        study-deadline close (б) on the same tick.
        """
        if self.study is None or self._lease_lost:
            return
        await self.ensure_lease()
        if self._finalized:
            return
        now = self.clock.now()
        ed = self.study.effective_deadline()

        # P5 first: tasks with deadline < ed and now > deadline → FAILED(deadline)
        await self._apply_p5(now, ed)
        if self._finalized:
            return

        await self._emit_study_budget_if_exhausted()
        if self._finalized:
            return

        all_done = bool(self.tasks) and all(
            t.state == TaskState.DONE for t in self.tasks.values()
        )
        trigger = pick_finalize_trigger(
            cancelled=self._study_cancelled,
            budget=self._budget_trigger_active(),
            refused=self._intake_refusal is not None,
            deadline=now >= ed,
            all_done=all_done,
        )
        if trigger in ("cancel", "budget", "deadline", "intake"):
            if trigger == "deadline":
                await self._close_remaining(
                    TaskReason.STUDY_DEADLINE, CloseReason.STUDY_DEADLINE
                )
            elif trigger == "budget" and not all_done:
                await self._close_remaining(TaskReason.BUDGET, CloseReason.BUDGET)
            await _finalize(self, trigger=trigger)
            return

        if self.tasks and all(t.state == TaskState.DONE for t in self.tasks.values()):
            await _finalize(self, trigger="all_done")
            return

        await self._fire_due_retries(now)

    async def _close_remaining(self, reason: TaskReason, close: CloseReason) -> None:
        from coscientist.core.cancel import _close_open_work

        events = _close_open_work(self, reason=reason, close=close)
        if events:
            await self._append(events)

    async def _apply_p5(self, now: datetime, ed: datetime) -> None:
        assert self.study is not None
        sid = self.study.study_id
        for task in list(self.tasks.values()):
            if task.state == TaskState.DONE:
                continue
            if not (now > task.deadline and task.deadline < ed):
                continue
            events: list[EventEnvelope] = []
            for aid in task.attempts:
                if not self.attempts.get(aid, {}).get("closed"):
                    events.append(
                        EventEnvelope(
                            study_id=sid,
                            type="AttemptClosed",
                            entity_key=aid,
                            idem_key=make_idem(
                                "AttemptClosed", aid, CloseReason.DEADLINE.value
                            ),
                            payload={
                                "attempt_id": aid,
                                "closed": True,
                                "close_reason": CloseReason.DEADLINE.value,
                                "task_id": task.task_id,
                            },
                            ts=now,
                            schema_version=2,
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
                        "outcome": TaskOutcome.FAILED.value,
                        "reason": TaskReason.DEADLINE.value,
                    },
                    ts=now,
                    schema_version=2,
                )
            )
            await self._append(events)

    async def _fire_due_retries(self, now: datetime) -> None:
        if self._lease_lost or self._finalized:
            return
        ed = self.study.effective_deadline() if self.study else None
        for tid, retry_at in list(self._retry_at.items()):
            if self._lease_lost or self._finalized:
                return
            if now < retry_at:
                continue
            task = self.tasks.get(tid)
            if task is None or task.state == TaskState.DONE:
                self._retry_at.pop(tid, None)
                continue
            if now > task.deadline or (ed is not None and now >= ed):
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
