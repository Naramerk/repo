"""HITL + fork extensions for StudyRuntime (keeps machine.py under size budget)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from coscientist.core.costly_gate import costly_gate
from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.domain import Clarification, HitlRequest, Task, TaskOutcome, TaskState
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.ids import hitl_request_id, study_id

if TYPE_CHECKING:
    from coscientist.core.machine import StudyRuntime


class HitlForkMixin:
    """Mixin: open/answer HITL, idle grace, continue-session fork stub."""

    async def open_hitl(
        self,
        task_key_or_id: str,
        kind: str = "plan_approval",
        timeout_s: int | None = None,
        *,
        cost_rub: float = 0.0,
    ) -> str:
        task = self._resolve_task(task_key_or_id)
        assert self.study is not None
        now = self.clock.now()
        gate = self.study.hitl_policy.gate(kind)
        to = timeout_s if timeout_s is not None else gate.timeout_s
        deadline = min(now + timedelta(seconds=to), task.deadline, self.study.deadline)
        rid = hitl_request_id()
        req = HitlRequest(
            request_id=rid,
            kind=kind,  # type: ignore[arg-type]
            payload_ref=task.task_id,
            deadline=deadline,
            study_id=self.study.study_id,
            task_id=task.task_id,
            open=True,
            cost_rub=float(cost_rub),
        )
        await self._append(
            [
                EventEnvelope(
                    study_id=self.study.study_id,
                    type="HitlOpened",
                    entity_key=rid,
                    idem_key=make_idem("HitlOpened", rid),
                    payload=req.model_dump(mode="json"),
                    ts=now,
                )
            ]
        )
        return rid

    async def answer_hitl(
        self,
        request_id: str,
        decision: str = "accept",
        *,
        critic_accepts: bool = True,
    ) -> None:
        h = self.hitl[request_id]
        if not h.open:
            return
        if h.kind == "costly_or_nonreal":
            answer = {
                "accept": "approve",
                "approve": "approve",
                "reject": "reject",
                "delegate": "delegate",
            }.get(decision)
            if answer is None:
                answer = "approve" if decision == "accept" else decision  # type: ignore[assignment]
            await self._apply_costly_or_nonreal(
                h, answer=answer, critic_accepts=critic_accepts  # type: ignore[arg-type]
            )
            return
        now = self.clock.now()
        await self._append(
            [
                EventEnvelope(
                    study_id=h.study_id,
                    type="HitlDecided",
                    entity_key=request_id,
                    idem_key=make_idem("HitlDecided", request_id),
                    payload={
                        "request_id": request_id,
                        "decision": decision,
                        "by": "human",
                        "decided_by": "human",
                        "approved_by": "human",
                        "reason": "answered",
                        "spent_rub": 0.0,
                    },
                    ts=now,
                )
            ]
        )

    def _budget_left_rub(self) -> float:
        assert self.study is not None
        bud = self.study.budget
        if bud is None or bud.delegated_costly is None:
            return 0.0
        spent = float(getattr(self, "_delegated_costly_spent", 0.0) or 0.0)
        return max(0.0, float(bud.delegated_costly) - spent)

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
        left = self._budget_left_rub()
        effect, ev = costly_gate(
            mode, answer, cost_rub=cost, budget_left_rub=left, critic_accepts=critic_accepts
        )
        now = self.clock.now()
        spent = float(ev.get("spent_rub") or 0.0)

        if effect == "WAIT":
            # Gate stays open (delegation disabled under fail mode).
            return

        # Prefer HitlDecided when there is a decided_by; otherwise HitlTimedOut alone.
        use_decided = answer is not None or ev.get("decided_by") is not None
        if answer is None:
            await self._append(
                [
                    EventEnvelope(
                        study_id=h.study_id,
                        type="HitlTimedOut",
                        entity_key=h.request_id,
                        idem_key=make_idem("HitlTimedOut", h.request_id),
                        payload={
                            "request_id": h.request_id,
                            "reason": ev["reason"],
                            "decided_by": ev.get("decided_by"),
                            "delegated_by": ev.get("delegated_by"),
                            # spent_rub lives on HitlDecided when critic decides after timeout
                            "spent_rub": 0.0 if use_decided else spent,
                            "effect": effect,
                        },
                        ts=now,
                    )
                ]
            )
        if use_decided:
            await self._append(
                [
                    EventEnvelope(
                        study_id=h.study_id,
                        type="HitlDecided",
                        entity_key=h.request_id,
                        idem_key=make_idem(
                            "HitlDecided", h.request_id, str(ev.get("reason")), str(spent)
                        ),
                        payload={
                            "request_id": h.request_id,
                            "decision": (
                                "accept"
                                if effect == "RUN"
                                else (
                                    "reject"
                                    if effect.startswith("SKIPPED")
                                    or effect.startswith("FAILED")
                                    else "delegate"
                                )
                            ),
                            "by": "human" if ev.get("decided_by") == "human" else "policy",
                            "decided_by": ev.get("decided_by"),
                            "approved_by": (
                                "human"
                                if ev.get("decided_by") == "human"
                                else ("critic" if ev.get("decided_by") == "critic" else None)
                            ),
                            "delegated_by": ev.get("delegated_by"),
                            "reason": ev["reason"],
                            "spent_rub": spent,
                            "effect": effect,
                        },
                        ts=now,
                    )
                ]
            )

        task = self.tasks[h.task_id]
        if effect == "RUN":
            return
        if effect == "FAILED(human_timeout)":
            if task.state != TaskState.DONE:
                await self._fail_task_human_timeout(h, task)
            return
        if effect.startswith("SKIPPED"):
            reason = "budget" if "budget" in effect else (
                "human_rejected" if "human_rejected" in effect else "critic_rejected"
            )
            if task.state != TaskState.DONE:
                await self._append(
                    [
                        EventEnvelope(
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
                EventEnvelope(
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

    async def _hitl_timeout(self, h: HitlRequest) -> None:
        if h.kind == "costly_or_nonreal":
            await self._apply_costly_or_nonreal(h, answer=None)
            return
        now = self.clock.now()
        await self._append(
            [
                EventEnvelope(
                    study_id=h.study_id,
                    type="HitlTimedOut",
                    entity_key=h.request_id,
                    idem_key=make_idem("HitlTimedOut", h.request_id),
                    payload={
                        "request_id": h.request_id,
                        "reason": "human_timeout",
                        "spent_rub": 0.0,
                    },
                    ts=now,
                )
            ]
        )
        if h.kind not in ("plan_approval", "spec_approval", "plan", "hypotheses", "report"):
            return
        policy = self.study.hitl_policy if self.study else None
        gate = policy.gate(h.kind) if policy else None
        on_timeout = gate.on_timeout if gate else "delegate"
        if on_timeout in ("delegate", "delegate_capped"):
            # Non-costly gates: delegate_capped behaves as delegate (no RUB cost).
            await self._critic_decide(h, decision="accept", delegated_by="timeout")
            return
        task = self.tasks[h.task_id]
        if task.state != TaskState.DONE:
            await self._fail_task_human_timeout(h, task)

    async def _critic_decide(
        self,
        h: HitlRequest,
        *,
        decision: str,
        delegated_by: str,
    ) -> None:
        """Critic decision path for on_timeout=delegate (DESIGN §7.3)."""
        now = self.clock.now()
        await self._append(
            [
                EventEnvelope(
                    study_id=h.study_id,
                    type="HitlDecided",
                    entity_key=h.request_id,
                    idem_key=make_idem("HitlDecided", h.request_id, "critic"),
                    payload={
                        "request_id": h.request_id,
                        "decision": decision,
                        "by": "policy",
                        "decided_by": "critic",
                        "approved_by": "critic",
                        "delegated_by": delegated_by,
                        "reason": f"timeout→agent ({delegated_by})",
                        "spent_rub": 0.0,
                    },
                    ts=now,
                )
            ]
        )

    def _depends_transitively(self, task: Task, key: str) -> bool:
        seen: set[str] = set()

        def walk(k: str) -> bool:
            if k == key:
                return True
            if k in seen:
                return False
            seen.add(k)
            matches = [t for t in self.tasks.values() if t.spec.task_key == k]
            if not matches:
                return False
            for d in matches[0].spec.depends_on:
                if walk(d):
                    return True
            return False

        return any(walk(d) for d in task.spec.depends_on)

    async def recover_after_idle(self, grace_s: float = 60.0) -> None:
        """Extend open HITL deadlines within task/study bounds."""
        if self.study is None:
            return
        now = self.clock.now()
        for h in list(self.hitl.values()):
            if not h.open:
                continue
            task = self.tasks[h.task_id]
            new_dl = min(
                max(h.deadline, now + timedelta(seconds=grace_s)),
                task.deadline,
                self.study.deadline,
            )
            if new_dl == h.deadline:
                continue
            await self._append(
                [
                    EventEnvelope(
                        study_id=h.study_id,
                        type="HitlDeadlineExtended",
                        entity_key=h.request_id,
                        idem_key=make_idem(
                            "HitlDeadlineExtended", h.request_id, new_dl.isoformat()
                        ),
                        payload={
                            "request_id": h.request_id,
                            "deadline": new_dl.isoformat(),
                        },
                        ts=now,
                    )
                ]
            )

    async def fork(
        self, clarifications: list[Clarification], *, nonce: str = ""
    ) -> StudyRuntime:
        """M0 stub: compute affected subgraph; full fork lands in M2."""
        assert self.study is not None
        digest = sha256_hex(
            canonical_json(
                [c.model_dump(mode="json") for c in clarifications] + [nonce]
            )
        )
        child_id = study_id()
        affected: set[str] = set()
        for c in clarifications:
            if c.target is None:
                affected.update({"SPEC", "PLAN"})
            else:
                affected.add(c.target)
        changed = True
        while changed:
            changed = False
            for t in self.tasks.values():
                if t.spec.task_key in affected:
                    continue
                if any(d in affected for d in t.spec.depends_on):
                    affected.add(t.spec.task_key)
                    changed = True
        self._last_fork = {  # type: ignore[attr-defined]
            "parent": self.study.study_id,
            "child": child_id,
            "affected": sorted(affected),
            "idem": f"fork:{self.study.study_id}:{digest}:{nonce}",
        }
        return self

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
        for h in self.hitl.values():
            if h.deadline > sd:
                return False
        return True
