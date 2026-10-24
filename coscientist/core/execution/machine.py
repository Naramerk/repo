"""Task state machine + study runtime shell (DESIGN §3.3).

Attempt lifecycle lives in ``attempts``; local fold in ``local_project``.
create/accept_plan live in ``lifecycle``, composed only via ``api.runtime``.
Finalizer/cancel in finalize/cancel. HITL / costly / fork / report are
``coscientist.experimental``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from coscientist.core.claims.graph import GraphStore, KindRegistry, default_execution_registry
from coscientist.core.claims.reasons import CloseReason, TaskReason
from coscientist.core.events.clock import Clock, SystemClock
from coscientist.core.events.errors import LeaseLost
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.schema_versions import (
    ATTEMPT_CLOSED_SCHEMA_VERSION,
    BUDGET_EXCEEDED_SCHEMA_VERSION,
    TASK_DONE_SCHEMA_VERSION,
)
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.attempts import AttemptsMixin
from coscientist.core.execution.domain import Provenance, Study, Task, TaskOutcome, TaskState
from coscientist.core.execution.local_project import LocalProjectMixin
from coscientist.core.finalize.finalize import (
    FinalizeTrigger,
    pick_finalize_trigger,
)
from coscientist.core.finalize.finalize import (
    finalize as _finalize,
)
from coscientist.core.finalize.study_view import study_view
from coscientist.core.plan.cancel import cancel_study as _cancel_study
from coscientist.core.plan.intake import IntakeRefuse


class StudyRuntime(LocalProjectMixin, AttemptsMixin):
    """In-process owner of one study: store, writer, tasks, timers.

    Lifecycle (create_study / accept_plan) is composed in ``coscientist.api.runtime``
    — this shell must not import ``core.lifecycle`` (ingest-gate isolation).
    """

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
        self.catalog: Any | None = None
        self.artifacts: Any | None = None
        self.dep_outputs: dict[str, dict[str, str]] = {}
        self.dep_provenance: dict[str, Provenance] = {}
        self.verdict_snapshots: dict[str, dict[str, Any]] = {}
        self.hypothesis_verdicts: dict[str, dict[str, Any]] = {}
        self._retry_at: dict[str, datetime] = {}
        self._finalized = False
        # True while finalize_loop_internal_error closes open work —
        # nested _append(TaskDone) must not re-enter expand/foreach/loop hooks.
        self._finalizing = False
        self.question_disposals: dict[str, dict] = {}
        # PR15: ArtifactRegistered(stamped) → artifact_id → kind (journal authority).
        self.artifact_kinds: dict[str, str] = {}
        # PR15: folded QuestionDisposeRejected rows (also in contributing_reasons).
        self._disposal_rejects: list[dict] = []
        # PR15: stamped kind with missing meta on replay (tainted / internal_error).
        self._stamp_meta_missing: list[dict] = []
        # Legacy journals with a second StudyFinalized (I3 breach) → taint mark.
        self._duplicate_study_finalized: list[dict] = []
        self._pending_kind_stamps: list[tuple[str, str]] = []
        self._study_cancelled = False
        self._budget_exceeded = False
        self._intake_refusal: IntakeRefuse | None = None
        self._study_spent_rub = Decimal("0")
        # PR12: per-task budget_share caps / spend (folded from AttemptStarted / Debit).
        self._task_budget_alloc: dict[str, Decimal] = {}
        self._task_spent_rub: dict[str, Decimal] = {}
        # PR9 expansion accounting (folded from PlanExpanded / ExpansionRefused).
        self._agent_expansions = 0
        self._human_expansions = 0
        self._replan_used_s = 0.0
        self._expand_pending: set[str] = set()
        self._expand_requests: dict[str, dict[str, Any]] = {}
        self._expansion_refusals: list[dict[str, Any]] = []
        self._hitl_decisions: dict[str, dict[str, Any]] = {}
        self._hitl_requests: dict[str, dict[str, Any]] = {}
        self._hitl_expand_used: set[str] = set()
        # PR10: foreach_template → concrete EXP keys (folded from PlanExpanded).
        self._foreach_children: dict[str, list[str]] = {}
        # PR11: LoopSpec runtime (folded from PlanExpanded(loop) / LoopCapped).
        self._loop_state: dict[str, dict[str, Any]] = {}
        self._loop_capped: dict[str, dict[str, Any]] = {}
        self._loop_done: set[str] = set()
        # PR20: CheckProposed / Approved / Rejected (folded from journal).
        self._check_proposals: dict[str, dict[str, Any]] = {}
        self._observations: list[dict[str, Any]] = []

    def bind_executor(self, executor: Any) -> Any:
        """Wire production ``graph`` into Executor (laundering + about edges)."""
        executor.get_graph = lambda: self.graph
        self.executor = executor
        return executor

    def make_executor(
        self,
        catalog: Any,
        artifacts: Any,
        *,
        get_attempt: Any | None = None,
        get_task: Any | None = None,
        on_events: Any | None = None,
        review_statuses: dict[str, Any] | None = None,
    ) -> Any:
        """Production factory: build Executor and bind live ``get_graph``."""
        from coscientist.core.execution.executor import Executor

        def _default_attempt(aid: str) -> Any:
            d = self.attempts[aid]
            return type(
                "Att",
                (),
                {
                    "attempt_id": d["attempt_id"],
                    "closed": d["closed"],
                    "provenance": d["provenance"],
                    "task_id": d["task_id"],
                    "study_id": d["study_id"],
                    "inputs": list(d["inputs"]),
                    "agent_output_digest": d.get("agent_output_digest"),
                    "charged": d["charged"],
                    "n": d.get("n"),
                    "allocated_budget_rub": d.get("allocated_budget_rub"),
                    "budget_limit_rub": d.get("budget_limit_rub"),
                },
            )()

        from coscientist.core.plan.shares import make_budget_debit_cb

        ex = Executor(
            self.writer,
            catalog,
            artifacts,
            clock=self.clock,
            get_attempt=get_attempt or _default_attempt,
            get_task=get_task or (lambda tid: self.tasks[tid]),
            on_events=on_events or self._project_local,
            review_statuses=review_statuses,
            get_graph=lambda: self.graph,
            get_results=lambda: self.results,
            get_artifact_kinds=lambda: self.artifact_kinds,
            get_proposals=lambda: getattr(self, "_check_proposals", {}) or {},
            max_attempts=self.max_attempts,
            on_budget_debit=make_budget_debit_cb(self),
        )
        # PR20: proposed_check prereg-before-attempt needs attempt seq map.
        ex._runtime_attempts = self.attempts  # type: ignore[attr-defined]
        self.artifacts = artifacts
        self.catalog = catalog
        # Fold may have deferred kind stamps (open_runtime projects before bind).
        flush = getattr(self, "_flush_pending_kind_stamps", None)
        if callable(flush):
            flush()
        self.bind_executor(ex)
        # Executor writes via writer (not StudyRuntime._append); wrap to run H-g.
        orig_append = ex._append

        async def _append_with_expand(
            events: list[EventEnvelope],
            *,
            require_open_attempt: str | None = None,
        ) -> None:
            await orig_append(events, require_open_attempt=require_open_attempt)
            if self._finalized or self._lease_lost or self._finalizing:
                return
            from coscientist.core.plan.expand import maybe_expand_after_task_done
            from coscientist.core.plan.foreach import maybe_foreach_after_task_done
            from coscientist.core.plan.loop import maybe_loop_after_task_done

            for ev in events:
                if ev.type != "TaskDone":
                    continue
                t = self.tasks.get(ev.entity_key)
                reason = getattr(t, "reason", None) if t is not None else None
                rv = getattr(reason, "value", reason)
                if rv == TaskReason.SUPERSEDED.value:
                    # Foreach template: expand_after only; always advance loops.
                    kids = getattr(self, "_foreach_children", None) or {}
                    if t is not None and t.spec.task_key in kids:
                        await maybe_expand_after_task_done(self, ev.entity_key)
                    await maybe_loop_after_task_done(self, ev.entity_key)
                    continue
                await maybe_foreach_after_task_done(self, ev.entity_key)
                await maybe_loop_after_task_done(self, ev.entity_key)
                await maybe_expand_after_task_done(self, ev.entity_key)

        ex._append = _append_with_expand  # type: ignore[method-assign]
        return ex

    async def _append(self, events: list[EventEnvelope]) -> None:
        if self._lease_lost:
            raise LeaseLost("writer stopped after lease loss")
        try:
            # Project returned envelopes (with seq) so AttemptStarted._seq /
            # CheckProposed.proposed_seq are available for PR20 prereg checks.
            stored = await self.writer.append_many(events)
        except LeaseLost:
            self._lease_lost = True
            raise
        # Project in durable append order (no ``asyncio.sleep(0)`` — see Executor._append).
        done_ids: list[str] = []
        for ev in stored:
            self._project_local(ev)
            if ev.type == "TaskDone":
                done_ids.append(ev.entity_key)
        # PR10 foreach → PR11 loops → H-g expand (expansion_request / expand_after).
        if (
            done_ids
            and not self._finalized
            and not self._lease_lost
            and not self._finalizing
        ):
            from coscientist.core.plan.expand import maybe_expand_after_task_done
            from coscientist.core.plan.foreach import maybe_foreach_after_task_done
            from coscientist.core.plan.loop import maybe_loop_after_task_done

            for tid in done_ids:
                # Superseded non-foreach: skip expand (same-batch PlanExpanded).
                # Foreach template SUPERSEDED counts as done for expand_after
                # (consistent with REFUSED).
                t = self.tasks.get(tid)
                reason = getattr(t, "reason", None) if t is not None else None
                rv = getattr(reason, "value", reason)
                if rv == TaskReason.SUPERSEDED.value:
                    kids = getattr(self, "_foreach_children", None) or {}
                    if t is not None and t.spec.task_key in kids:
                        await maybe_expand_after_task_done(self, tid)
                    await maybe_loop_after_task_done(self, tid)
                    continue
                await maybe_foreach_after_task_done(self, tid)
                await maybe_loop_after_task_done(self, tid)
                await maybe_expand_after_task_done(self, tid)

    def view(self):
        from coscientist.core.claims.dispositions import dispositions

        qdisp = None
        if self.plans:
            from coscientist.core.plan.loop import capped_scope_questions

            arts = getattr(self, "artifacts", None)
            lookup = getattr(arts, "get_meta", None) if arts is not None else None
            qdisp = dispositions(
                tasks=self.tasks,
                plans=self.plans,
                graph=self.graph,
                study=self.study,
                results=self.results,
                artifact_lookup=lookup if callable(lookup) else None,
                loop_capped_questions=capped_scope_questions(self),
                question_disposals=getattr(self, "question_disposals", None),
                journal_kinds=getattr(self, "artifact_kinds", None),
            )
        ed = self.study.effective_deadline() if self.study else None
        ftypes = None
        if self.plans:
            from coscientist.core.claims.rubric import question_final_types

            ftypes = question_final_types(self.plans)
        return study_view(
            self.tasks.values(),
            open_hitl=0,
            attempts=self.attempts,
            finalized=self._finalized,
            now=self.clock.now(),
            effective_deadline=ed,
            question_dispositions=qdisp,
            question_final_types=ftypes,
        )

    def invariant_open_attempts(self) -> bool:
        for t in self.tasks.values():
            open_n = sum(1 for a in t.attempts if not self.attempts.get(a, {}).get("closed"))
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
            if self.store.epoch is None:
                from coscientist.core.events.errors import LeaseLost

                raise LeaseLost("no lease epoch set")
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
                    schema_version=BUDGET_EXCEEDED_SCHEMA_VERSION,
                )
            ]
        )

    async def record_budget_debit(
        self,
        *,
        spent_rub: Decimal | float | int | str,
        request_id: str,
        extra: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> None:
        """Public writer for ``BudgetDebited`` (study + optional task_id share)."""
        if self.study is None:
            raise RuntimeError("no study")
        spent = Decimal(str(spent_rub))
        payload: dict[str, Any] = {
            "spent_rub": str(spent),
            "request_id": request_id,
            **(extra or {}),
        }
        if task_id:
            payload["task_id"] = task_id
        await self._append(
            [
                EventEnvelope(
                    study_id=self.study.study_id,
                    type="BudgetDebited",
                    entity_key=request_id,
                    idem_key=make_idem(
                        "BudgetDebited",
                        request_id,
                        str(spent),
                        str(payload.get("actual_rub", spent)),
                    ),
                    payload=payload,
                    ts=self.clock.now(),
                    schema_version=writer_schema_version('BudgetDebited'),
                )
            ]
        )
        if task_id:
            from coscientist.core.plan.shares import enforce_task_budget_share

            await enforce_task_budget_share(self, task_id)

    def _study_budget_exhausted(self) -> bool:
        """True when folded ``BudgetDebited.spent_rub`` reaches ``Budget.max_cost_rub``.

        Reaching the cap (``>=``) counts as exhausted — aligned with DESIGN §7.4.
        """
        if self.study is None or self.study.budget is None:
            return False
        bud = self.study.budget
        return bud.max_cost_rub is not None and self._study_spent_rub >= bud.max_cost_rub

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
        # PR20: delegated review timeout ≡ reject (core path; experimental also runs).
        from coscientist.core.plan.proposed_check import reject_stale_proposed_check_reviews

        await reject_stale_proposed_check_reviews(self)
        if self._finalized:
            return
        now = self.clock.now()
        ed = self.study.effective_deadline()

        # P5 first: tasks with deadline < ed and now > deadline → FAILED(deadline)
        await self._apply_p5(now, ed)
        if self._finalized:
            return

        # Budget before H-c so trigger priority budget > … > all_done holds.
        await self._emit_study_budget_if_exhausted()

        # H-c while still running: skip children of FAILED/SKIPPED parents
        # without InputSpec (stage chain excluded — see AttemptsMixin).
        # Skip when budget/cancel already owns close reasons (else children get
        # input_unavailable instead of budget/cancelled).
        if now < ed and not self._budget_exceeded and not self._study_cancelled:
            await self._skip_unavailable_dep_children()

        all_done = bool(self.tasks) and all(t.state == TaskState.DONE for t in self.tasks.values())
        trigger = pick_finalize_trigger(
            cancelled=self._study_cancelled,
            budget=self._budget_trigger_active(),
            refused=self._intake_refusal is not None,
            deadline=now >= ed,
            all_done=all_done,
        )
        if trigger in ("cancel", "budget", "deadline", "intake"):
            if trigger == "deadline":
                await self._close_remaining(TaskReason.STUDY_DEADLINE, CloseReason.STUDY_DEADLINE)
            elif trigger == "budget" and not all_done:
                await self._close_remaining(TaskReason.BUDGET, CloseReason.BUDGET)
            elif trigger == "cancel" and not all_done:
                await self._close_remaining(TaskReason.CANCELLED, CloseReason.CANCELLED)
            await _finalize(self, trigger=trigger)
            return

        if self.tasks and all(t.state == TaskState.DONE for t in self.tasks.values()):
            await _finalize(self, trigger="all_done")
            return

        await self._fire_due_retries(now)

    async def _close_remaining(self, reason: TaskReason, close: CloseReason) -> None:
        from coscientist.core.plan.cancel import _close_open_work

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
                            idem_key=make_idem("AttemptClosed", aid, CloseReason.DEADLINE.value),
                            payload={
                                "attempt_id": aid,
                                "closed": True,
                                "close_reason": CloseReason.DEADLINE.value,
                                "task_id": task.task_id,
                            },
                            ts=now,
                            schema_version=ATTEMPT_CLOSED_SCHEMA_VERSION,
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
                    schema_version=TASK_DONE_SCHEMA_VERSION,
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
            open_atts = []
            for a in task.attempts:
                row = self.attempts.get(a)
                if row is None:
                    from coscientist.core.events.errors import JournalFormatError
                    raise JournalFormatError(f"attempt row missing: {a!r}")
                if not row.get("closed"):
                    open_atts.append(a)
            if open_atts:
                self._retry_at.pop(tid, None)
                continue
            # Inherit provenance only from a prior closed attempt (never invent "real").
            prov = (
                self._provenance_for_retry(tid) if task.attempts else None
            )
            self._retry_at.pop(tid, None)
            # None = honest refuse (zero share / empty pool); task already SKIPPED.
            await self.start_attempt(tid, provenance=prov)

def new_owner() -> str:
    return str(uuid.uuid4())
