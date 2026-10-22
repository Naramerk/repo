"""HITL + fork extensions for StudyRuntime (keeps machine.py under size budget)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.domain import Clarification, HitlRequest, Task, TaskOutcome, TaskState
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.ids import hitl_request_id, study_id

if TYPE_CHECKING:
    from coscientist.core.machine import StudyRuntime


class HitlForkMixin:
    """Mixin: open/answer HITL, idle grace, continue-session fork stub."""

    async def open_hitl(
        self: StudyRuntime,
        task_key_or_id: str,
        kind: str = "plan_approval",
        timeout_s: int | None = None,
    ) -> str:
        task = self._resolve_task(task_key_or_id)
        assert self.study is not None
        now = self.clock.now()
        to = timeout_s or self.study.hitl_policy.timeout_s.get(kind, 120)
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
        self: StudyRuntime, request_id: str, decision: str = "accept"
    ) -> None:
        h = self.hitl[request_id]
        if not h.open:
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
                        "reason": "answered",
                    },
                    ts=now,
                )
            ]
        )

    async def _hitl_timeout(self: StudyRuntime, h: HitlRequest) -> None:
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
                    },
                    ts=now,
                )
            ]
        )
        if h.kind not in ("plan_approval", "spec_approval"):
            return
        # M0: on_timeout=delegate → close gate without failing (critic stub);
        # on_timeout=fail → FAILED(human_timeout) + descendants SKIPPED.
        policy = self.study.hitl_policy if self.study else None
        on_timeout = policy.on_timeout if policy else "delegate"
        if on_timeout == "delegate":
            return
        task = self.tasks[h.task_id]
        if task.state != TaskState.DONE:
            await self._append(
                [
                    EventEnvelope(
                        study_id=h.study_id,
                        type="TaskDone",
                        entity_key=h.task_id,
                        idem_key=make_idem("TaskDone", h.task_id),
                        payload={
                            "task_id": h.task_id,
                            "outcome": TaskOutcome.FAILED.value,
                            "reason": "human_timeout",
                        },
                        ts=now,
                    )
                ]
            )
            for t in list(self.tasks.values()):
                if t.state == TaskState.DONE:
                    continue
                if task.spec.task_key in t.spec.depends_on or self._depends_transitively(
                    t, task.spec.task_key
                ):
                    await self.cancel_task(t.task_id, reason="input_unavailable")

    def _depends_transitively(self: StudyRuntime, task: Task, key: str) -> bool:
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

    async def recover_after_idle(self: StudyRuntime, grace_s: float = 60.0) -> None:
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
        self: StudyRuntime, clarifications: list[Clarification], *, nonce: str = ""
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

    def invariant_open_attempts(self: StudyRuntime) -> bool:
        for t in self.tasks.values():
            open_n = sum(
                1 for a in t.attempts if not self.attempts.get(a, {}).get("closed")
            )
            if open_n > 1:
                return False
        return True

    def invariant_deadlines(self: StudyRuntime) -> bool:
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
