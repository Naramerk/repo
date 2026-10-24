"""Costly_or_nonreal HITL gate (experimental / pre-M2)."""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from coscientist.core.claims.reasons import TaskReason
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.mapping import optional_item
from coscientist.core.events.schema_versions import TASK_DONE_SCHEMA_VERSION
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.domain import HitlRequest, Task, TaskOutcome, TaskState
from coscientist.experimental.costly import (
    DelegatedBudget,
    costly_gate,
    settle,
)


class CostlyEffect(str, Enum):
    """Structured gate outcome — callers match on this, not string prefixes."""

    RUN = "RUN"
    WAIT = "WAIT"
    FAILED_HUMAN_TIMEOUT = "FAILED(human_timeout)"
    SKIPPED_BUDGET = "SKIPPED(budget)"
    SKIPPED_HUMAN_REJECTED = "SKIPPED(human_rejected)"
    SKIPPED_CRITIC_REJECTED = "SKIPPED(critic_rejected)"


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


_SKIPPED_EFFECTS = frozenset(
    {
        CostlyEffect.SKIPPED_BUDGET,
        CostlyEffect.SKIPPED_HUMAN_REJECTED,
        CostlyEffect.SKIPPED_CRITIC_REJECTED,
    }
)


def _budget_state(budget):
    if budget:
        return budget.to_state()
    return None



def _decision_for_effect(effect: CostlyEffect) -> str:
    if effect is CostlyEffect.RUN:
        return "accept"
    if any((effect is CostlyEffect.FAILED_HUMAN_TIMEOUT, effect in _SKIPPED_EFFECTS)):
        return "reject"
    if effect is CostlyEffect.WAIT:
        return "delegate"
    raise ValueError(f"unknown CostlyEffect: {effect!r}")


def _skip_reason(effect: CostlyEffect) -> str:
    if effect is CostlyEffect.SKIPPED_BUDGET:
        return "budget"
    if effect is CostlyEffect.SKIPPED_HUMAN_REJECTED:
        return "human_rejected"
    if effect is CostlyEffect.SKIPPED_CRITIC_REJECTED:
        return "critic_rejected"
    raise ValueError(f"not a SKIPPED CostlyEffect: {effect!r}")


def _require_float(value: object, *, what: str) -> float:
    if type(value) is not float and type(value) is not int:
        raise JournalFormatError(f"{what} must be int|float, got {type(value).__name__}")
    return (value + 0.0)


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
        reason: str | TaskReason,
    ) -> None:
        raise NotImplementedError

    def _depends_transitively(self, task: Task, key: str) -> bool:
        raise NotImplementedError

    def _ensure_delegated_budget(self) -> DelegatedBudget | None:
        if self.delegated_budget is not None:
            return self.delegated_budget
        if self.study is None:
            raise JournalFormatError("costly gate requires study")
        bud = self.study.budget
        if any((bud is None, bud.delegated_costly is None)):
            return None
        self.delegated_budget = DelegatedBudget(
            _require_float(bud.delegated_costly, what="budget.delegated_costly")
        )
        return self.delegated_budget

    async def settle_delegated_spend(self, request_id: str, actual_rub: float) -> dict:
        """Debit actual spend after a delegated attempt; persist via BudgetDebited."""
        if self.study is None:
            raise JournalFormatError("settle_delegated_spend requires study")
        pending = optional_item(self._pending_costly_events, request_id)
        if not pending:
            return {"spent_rub": 0.0}
        budget = self._ensure_delegated_budget()
        settled = settle(dict(pending), budget, actual_rub)
        core: _StudyBudgetEmitter
        core = self  # type: ignore[assignment]
        await core.record_budget_debit(
            spent_rub=settled["spent_rub"],
            request_id=request_id,
            extra={
                "actual_rub": actual_rub,
                "cost_estimate_rub": optional_item(pending, "cost_estimate_rub"),
                "decided_by": optional_item(settled, "decided_by"),
                "delegated_by": optional_item(settled, "delegated_by"),
                "reason": optional_item(settled, "reason"),
                "budget": _budget_state(budget),
            },
        )
        if request_id in self._pending_costly_events:
            self._pending_costly_events.pop(request_id)
        # Study-level Budget.max_cost_rub: costly spend counts toward the study.
        await core._emit_study_budget_if_exhausted()
        return settled

    async def _apply_costly_or_nonreal(
        self,
        h: HitlRequest,
        *,
        answer: str | None,
        critic_accepts: bool,
    ) -> None:
        if type(critic_accepts) is not bool:
            raise TypeError(
                f"critic_accepts must be bool, got {type(critic_accepts).__name__}"
            )
        if self.study is None:
            raise JournalFormatError("costly_or_nonreal requires study")
        gate = self.study.hitl_policy.gate("costly_or_nonreal")
        if gate is None:
            raise ValueError("HITL kind 'costly_or_nonreal' has no gate in hitl_policy")
        mode = gate.on_timeout
        cost = _require_float(h.cost_rub, what="HitlRequest.cost_rub")
        budget = self._ensure_delegated_budget()
        effect_raw, ev = costly_gate(
            mode,
            answer,
            cost_estimate_rub=cost,
            budget=budget,
            critic_accepts=critic_accepts,
        )
        if isinstance(effect_raw, CostlyEffect):
            effect = effect_raw
        elif isinstance(effect_raw, str):
            effect = CostlyEffect(effect_raw)
        else:
            raise TypeError(f"costly_gate must return CostlyEffect|str, got {type(effect_raw)!r}")
        now = self.clock.now()
        effect_s = effect.value
        if not isinstance(effect_s, str):
            raise TypeError(f"CostlyEffect.value must be str, got {type(effect_s).__name__}")

        if effect is CostlyEffect.WAIT:
            reason = ev["reason"]
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
                            reason,
                            now.isoformat(),
                        ),
                        payload={
                            "request_id": h.request_id,
                            "decision": "delegate",
                            "by": "human",
                            "decided_by": optional_item(ev, "decided_by"),
                            "delegated_by": optional_item(ev, "delegated_by"),
                            "reason": reason,
                            "spent_rub": 0.0,
                            "human_message": optional_item(ev, "human_message"),
                            "effect": effect_s,
                            "keep_open": True,
                        },
                        ts=now,
                        schema_version=writer_schema_version('HitlDecided'),
                    )
                ]
            )
            return

        use_decided = answer is not None or optional_item(ev, "decided_by") is not None
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
                            "decided_by": optional_item(ev, "decided_by"),
                            "delegated_by": optional_item(ev, "delegated_by"),
                            "spent_rub": 0.0,
                            "human_message": optional_item(ev, "human_message"),
                            "effect": effect_s,
                        },
                        ts=now,
                        schema_version=writer_schema_version('HitlTimedOut'),
                    )
                ]
            )
        if use_decided:
            decided_by = optional_item(ev, "decided_by")
            if decided_by == "human":
                by = "human"
                approved_by = "human"
            elif decided_by == "critic":
                by = "policy"
                approved_by = "critic"
            elif decided_by is None:
                by = "policy"
                approved_by = None
            else:
                raise JournalFormatError(f"unknown decided_by={decided_by!r}")
            payload = {
                "request_id": h.request_id,
                "decision": _decision_for_effect(effect),
                "by": by,
                "decided_by": decided_by,
                "approved_by": approved_by,
                "delegated_by": optional_item(ev, "delegated_by"),
                "reason": ev["reason"],
                "spent_rub": 0.0,
                "cost_estimate_rub": ev["cost_estimate_rub"],
                "human_message": optional_item(ev, "human_message"),
                "effect": effect_s,
            }
            await self._append(
                [
                    UnstampedEvent(
                        study_id=h.study_id,
                        type="HitlDecided",
                        entity_key=h.request_id,
                        idem_key=make_idem(
                            "HitlDecided", h.request_id, ev["reason"], effect_s
                        ),
                        payload=payload,
                        ts=now,
                        schema_version=writer_schema_version('HitlDecided'),
                    )
                ]
            )
            if effect is CostlyEffect.RUN and decided_by == "critic":
                self._pending_costly_events[h.request_id] = dict(payload)

        task = self.tasks[h.task_id]
        if effect is CostlyEffect.RUN:
            return
        if effect is CostlyEffect.FAILED_HUMAN_TIMEOUT:
            if task.state != TaskState.DONE:
                await self._fail_task_human_timeout(h, task)
            return
        if effect in _SKIPPED_EFFECTS:
            reason = _skip_reason(effect)
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
                            schema_version=TASK_DONE_SCHEMA_VERSION,
                        )
                    ]
                )
                await self._skip_dependents(task)
            return
        raise ValueError(f"unhandled CostlyEffect: {effect!r}")

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
                    schema_version=TASK_DONE_SCHEMA_VERSION,
                )
            ]
        )
        await self._skip_dependents(task)

    async def _skip_dependents(self, task: Task) -> None:
        for t in list(self.tasks.values()):
            if t.state == TaskState.DONE:
                continue
            if any((task.spec.task_key in t.spec.depends_on, self._depends_transitively(t, task.spec.task_key))):
                await self.cancel_task(t.task_id, reason="input_unavailable")
