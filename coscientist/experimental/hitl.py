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
        expand_after_task: str | None = None,
    ) -> str:
        """Open a HITL gate.

        Plan-change offers (``plan`` / ``plan_approval`` / ``plan_expansion``)
        open as ``kind=plan_expansion`` bound via ``expand_after_task`` so a
        human edit can produce ``PlanExpanded`` / ``ExpansionRefused``.
        """
        task = self._resolve_task(task_key_or_id)
        assert self.study is not None
        now = self.clock.now()
        # Plan approval with edits → plan_expansion path (bound after_task).
        gate_kind = kind
        after: str | None = expand_after_task
        if kind in ("plan", "plan_approval", "plan_expansion"):
            gate_kind = "plan_expansion"
            after = after or task.spec.task_key
        gate = self.study.hitl_policy.gate(gate_kind)
        to = timeout_s if timeout_s is not None else gate.timeout_s
        deadline = min(
            now + timedelta(seconds=to),
            task.deadline,
            self.study.effective_deadline(),
        )
        rid = hitl_request_id()
        req = HitlRequest(
            request_id=rid,
            kind=gate_kind,  # type: ignore[arg-type]
            payload_ref=task.task_id,
            deadline=deadline,
            study_id=self.study.study_id,
            task_id=task.task_id,
            open=True,
            cost_rub=float(cost_rub),
            expand_after_task=after,
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
        expansion: dict[str, Any] | None = None,
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
            await self._maybe_hitl_expand(h, request_id, expansion)
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
        if decision in ("accept", "approve"):
            await self._maybe_hitl_expand(h, request_id, expansion)

    async def _maybe_hitl_expand(
        self,
        h: HitlRequest,
        request_id: str,
        expansion: dict[str, Any] | None,
    ) -> None:
        """Third H-g trigger: HITL plan edit → PlanExpanded / ExpansionRefused.

        ``delegated_by`` is derived from the HitlDecided event, not an argument.
        Wrong/unknown kind with a plan change → ``ExpansionRefused(invalid_hitl)``
        (never silent drop).
        """
        if not expansion:
            return
        from coscientist.core.domain import QuestionSpec, QuestionSupersede, TaskSpec
        from coscientist.core.expand import expand_plan
        from coscientist.core.expand_trigger import refuse_expansion

        after = str(
            expansion.get("after_task")
            or getattr(h, "expand_after_task", None)
            or ""
        )
        if not after and h.task_id:
            task = self.tasks.get(h.task_id)
            after = task.spec.task_key if task is not None else h.task_id
        after = after or "HITL"
        if h.kind != "plan_expansion":
            await refuse_expansion(
                self,  # type: ignore[arg-type]
                after,
                reason="invalid_hitl",
                detail=f"hitl_wrong_kind:{h.kind or 'unknown'}",
            )
            return
        added_tasks = [
            TaskSpec.model_validate(t) if isinstance(t, dict) else t
            for t in (expansion.get("added_tasks") or [])
        ]
        added_questions = [
            QuestionSpec.model_validate(q) if isinstance(q, dict) else q
            for q in (expansion.get("added_questions") or [])
        ]
        superseded_questions = [
            QuestionSupersede.model_validate(s) if isinstance(s, dict) else s
            for s in (expansion.get("superseded_questions") or [])
        ]
        await expand_plan(
            self,  # type: ignore[arg-type]
            after_task=after,
            added_tasks=added_tasks,
            added_questions=added_questions,
            superseded_tasks=list(expansion.get("superseded_tasks") or []),
            superseded_questions=superseded_questions,
            origin="plan",
            reason=str(expansion.get("reason") or "hitl_plan_edit"),
            hitl_request_id=request_id,
            assign_keys=bool(expansion.get("assign_keys", False)),
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
            "plan_expansion",
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
        # Mark plan node when «decide yourself» / timeout delegate (graph attrs).
        await self._mark_plan_delegated(h, delegated_by=delegated_by)

    async def _mark_plan_delegated(self, h: HitlRequest, *, delegated_by: str) -> None:
        if h.kind not in ("plan", "plan_approval", "plan_expansion", "hypotheses"):
            return
        graph = getattr(self, "graph", None)
        plans = getattr(self, "plans", None)
        if graph is None or not isinstance(plans, dict) or not plans:
            return
        from coscientist.core.graph import GraphNode
        from coscientist.core.supersede import plan_node_id

        prev = max(plans.values(), key=lambda p: int(p.get("revision") or 0))
        pid = str(prev.get("plan_id") or "")
        if not pid:
            return
        sid = h.study_id
        pnid = plan_node_id(sid, pid)
        existing = graph.get_node(pnid)
        attrs = dict((existing.attrs if existing is not None else None) or {})
        attrs["delegated_by"] = delegated_by
        attrs["approved_by"] = "critic"
        now = self.clock.now()
        node = GraphNode(
            node_id=pnid,
            kind="plan",
            study_id=sid,
            ref_id=pid,
            attrs=attrs,
            created_at=getattr(existing, "created_at", None) or now,
        )
        await self._append(
            [
                EventEnvelope(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=pnid,
                    idem_key=make_idem("GraphNodeUpserted", pnid, "hitl_delegate", delegated_by),
                    payload=node.model_dump(mode="json"),
                    ts=now,
                    schema_version=1,
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
                self.study.effective_deadline(),
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
