"""HITL + fork extensions (experimental / pre-M2)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Self

from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.domain import Clarification, HitlRequest, Task
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.ids import hitl_request_id, study_id
from coscientist.experimental.hitl_costly import CostlyHitlMixin


class HitlForkMixin(CostlyHitlMixin):
    """Mixin: open/answer HITL, idle grace, continue-session fork stub."""

    attempts: dict[str, dict[str, Any]]

    def _resolve_task(self, task_key_or_id: str) -> Task:
        raise NotImplementedError

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
            }.get(decision, decision)
            await self._apply_costly_or_nonreal(
                h, answer=answer, critic_accepts=critic_accepts  # type: ignore[arg-type]
            )
            return
        # DESIGN §7.3: «реши сам» → critic with delegated_by=human
        if decision == "delegate":
            await self._critic_decide(h, decision="accept", delegated_by="human")
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
        if h.kind not in (
            "plan_approval",
            "spec_approval",
            "plan",
            "hypotheses",
            "report",
        ):
            return
        policy = self.study.hitl_policy if self.study else None
        gate = policy.gate(h.kind) if policy else None
        on_timeout = gate.on_timeout if gate else "delegate"
        if on_timeout in ("delegate", "delegate_capped"):
            await self._critic_decide(h, decision="accept", delegated_by="timeout")
            return
        task = self.tasks[h.task_id]
        from coscientist.core.domain import TaskState

        if task.state != TaskState.DONE:
            await self._fail_task_human_timeout(h, task)

    async def _critic_decide(
        self,
        h: HitlRequest,
        *,
        decision: str,
        delegated_by: str,
    ) -> None:
        """Critic path for delegate / on_timeout=delegate (DESIGN §7.3)."""
        now = self.clock.now()
        reason = (
            f"timeout→agent ({delegated_by})"
            if delegated_by == "timeout"
            else f"delegate→agent ({delegated_by})"
        )
        await self._append(
            [
                EventEnvelope(
                    study_id=h.study_id,
                    type="HitlDecided",
                    entity_key=h.request_id,
                    idem_key=make_idem("HitlDecided", h.request_id, "critic", delegated_by),
                    payload={
                        "request_id": h.request_id,
                        "decision": decision,
                        "by": "policy",
                        "decided_by": "critic",
                        "approved_by": "critic",
                        "delegated_by": delegated_by,
                        "reason": reason,
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
    ) -> Self:
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
