"""Costly_or_nonreal HITL gate (experimental / pre-M2)."""

from __future__ import annotations

from typing import Any, Protocol

from coscientist.core.claims.reasons import TaskReason
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.execution.domain import HitlRequest, Task, TaskOutcome, TaskState
from coscientist.experimental.costly import DelegatedBudget, costly_gate, settle


class _StudyBudgetEmitter(Protocol):
    """Core ``StudyRuntime`` surface used after a costly debit (no MRO stubs)."""

    async def record_budget_debit(
        self,
        *,
        spent_rub: Any,
        request_id: str,
        extra: dict[str, Any] | None = None,
    ) -> None: ...

    async def _emit_study_budget_if_exhausted(self) -> None: ...

    async def _append(self, events: list[UnstampedEvent]) -> None: ...


class CostlyHitlMixin:
    """Mixin for costly_or_nonreal gate + budget accounting."""

    study: Any
    hitl: dict[str, HitlRequest]
    tasks: dict[str, Task]
    clock: Any
    delegated_budget: DelegatedBudget | None
    _pending_costly_events: dict[str, dict[str, Any]]

    async def _append(self, events: list[UnstampedEvent]) -> None:
        raise NotImplementedError

    async def cancel_task(
        self,
        task_key_or_id: str,
        reason: str | TaskReason = TaskReason.CANCELLED,
    ) -> None:
        raise NotImplementedError

    def _depends_transitively(self, task: Task, key: str) -> bool:
        raise NotImplementedError

    def _ensure_delegated_budget(self) -> DelegatedBudget | None:
        if self.delegated_budget is not None:
            return self.delegated_budget
        assert self.study is not None
        bud = self.study.budget
        if bud is None or bud.delegated_costly is None:
            return None
        self.delegated_budget = DelegatedBudget(float(bud.delegated_costly))
        return self.delegated_budget

    async def settle_delegated_spend(self, request_id: str, actual_rub: float) -> dict:
        """Debit actual spend after a delegated attempt; persist via BudgetDebited."""
        assert self.study is not None
        pending = self._pending_costly_events.get(request_id)
        if not pending:
            return {"spent_rub": 0.0}
        budget = self._ensure_delegated_budget()
        settled = settle(dict(pending), budget, actual_rub)
        core: _StudyBudgetEmitter = self  # type: ignore[assignment]
        await core.record_budget_debit(
            spent_rub=settled["spent_rub"],
            request_id=request_id,
            extra={
                "actual_rub": actual_rub,
                "cost_estimate_rub": pending.get("cost_estimate_rub"),
                "decided_by": settled.get("decided_by"),
                "delegated_by": settled.get("delegated_by"),
                "reason": settled.get("reason"),
                "budget": budget.to_state() if budget else None,
            },
        )
        self._pending_costly_events.pop(request_id, None)
        # Study-level Budget.max_cost_rub: costly spend counts toward the study.
        await core._emit_study_budget_if_exhausted()
        return settled

    async def _apply_costly_or_nonreal(
        self,
        h: HitlRequest,
        *,
        answer: str | None,
        critic_accepts: bool = True,
    ) -> None:
        assert self.study is not None
        gate = self.study.hitl_policy.gate("costly_or_nonreal")
        mode = gate.on_timeout
        cost = float(h.cost_rub)
        budget = self._ensure_delegated_budget()
        effect, ev = costly_gate(
            mode,
            answer,
            cost_estimate_rub=cost,
            budget=budget,
            critic_accepts=critic_accepts,
        )
        now = self.clock.now()

        if effect == "WAIT":
            await self._append(
                [
                    UnstampedEvent(
                        study_id=h.study_id,
                        type="HitlDecided",
                        entity_key=h.request_id,
                        idem_key=make_idem(
                            "HitlDecided",
                            h.request_id,
                            "WAIT",
                            str(ev.get("reason")),
                            str(now),
                        ),
                        payload={
                            "request_id": h.request_id,
                            "decision": "delegate",
                            "by": "human",
                            "decided_by": ev.get("decided_by"),
                            "delegated_by": ev.get("delegated_by"),
                            "reason": ev["reason"],
                            "spent_rub": 0.0,
                            "human_message": ev.get("human_message"),
                            "effect": "WAIT",
                            "keep_open": True,
                        },
                        ts=now,
                    )
                ]
            )
            return

        use_decided = answer is not None or ev.get("decided_by") is not None
        if answer is None:
            await self._append(
                [
                    UnstampedEvent(
                        study_id=h.study_id,
                        type="HitlTimedOut",
                        entity_key=h.request_id,
                        idem_key=make_idem("HitlTimedOut", h.request_id),
                        payload={
                            "request_id": h.request_id,
                            "reason": ev["reason"],
                            "decided_by": ev.get("decided_by"),
                            "delegated_by": ev.get("delegated_by"),
                            "spent_rub": 0.0,
                            "human_message": ev.get("human_message"),
                            "effect": effect,
                        },
                        ts=now,
                    )
                ]
            )
        if use_decided:
            decided_by = ev.get("decided_by")
            payload = {
                "request_id": h.request_id,
                "decision": (
                    "accept"
                    if effect == "RUN"
                    else (
                        "reject"
                        if effect.startswith("SKIPPED") or effect.startswith("FAILED")
                        else "delegate"
                    )
                ),
                "by": "human" if decided_by == "human" else "policy",
                "decided_by": decided_by,
                "approved_by": (
                    "human"
                    if decided_by == "human"
                    else ("critic" if decided_by == "critic" else None)
                ),
                "delegated_by": ev.get("delegated_by"),
                "reason": ev["reason"],
                "spent_rub": 0.0,
                "cost_estimate_rub": ev.get("cost_estimate_rub", cost),
                "human_message": ev.get("human_message"),
                "effect": effect,
            }
            await self._append(
                [
                    UnstampedEvent(
                        study_id=h.study_id,
                        type="HitlDecided",
                        entity_key=h.request_id,
                        idem_key=make_idem(
                            "HitlDecided", h.request_id, str(ev.get("reason")), effect
                        ),
                        payload=payload,
                        ts=now,
                    )
                ]
            )
            if effect == "RUN" and decided_by == "critic":
                self._pending_costly_events[h.request_id] = dict(payload)

        task = self.tasks[h.task_id]
        if effect == "RUN":
            return
        if effect == "FAILED(human_timeout)":
            if task.state != TaskState.DONE:
                await self._fail_task_human_timeout(h, task)
            return
        if effect.startswith("SKIPPED"):
            reason = (
                "budget"
                if "budget" in effect
                else (
                    "human_rejected" if "human_rejected" in effect else "critic_rejected"
                )
            )
            if task.state != TaskState.DONE:
                await self._append(
                    [
                        UnstampedEvent(
                            study_id=h.study_id,
                            type="TaskDone",
                            entity_key=h.task_id,
                            idem_key=make_idem("TaskDone", h.task_id, reason),
                            payload={
                                "task_id": h.task_id,
                                "outcome": TaskOutcome.SKIPPED.value,
                                "reason": reason,
                            },
                            ts=now,
                        )
                    ]
                )
                await self._skip_dependents(task)

    async def _fail_task_human_timeout(self, h: HitlRequest, task: Task) -> None:
        now = self.clock.now()
        await self._append(
            [
                UnstampedEvent(
                    study_id=h.study_id,
                    type="TaskDone",
                    entity_key=h.task_id,
                    idem_key=make_idem("TaskDone", h.task_id, "human_timeout"),
                    payload={
                        "task_id": h.task_id,
                        "outcome": TaskOutcome.FAILED.value,
                        "reason": "human_timeout",
                    },
                    ts=now,
                )
            ]
        )
        await self._skip_dependents(task)

    async def _skip_dependents(self, task: Task) -> None:
        for t in list(self.tasks.values()):
            if t.state == TaskState.DONE:
                continue
            if task.spec.task_key in t.spec.depends_on or self._depends_transitively(
                t, task.spec.task_key
            ):
                await self.cancel_task(t.task_id, reason="input_unavailable")
