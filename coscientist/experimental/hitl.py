"""HITL + fork extensions (experimental / pre-M2)."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any, Self

from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import Conflict
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.ids import hitl_request_id, study_id
from coscientist.core.execution.domain import Clarification, HitlRequest, Task
from coscientist.experimental.hitl_costly import CostlyHitlMixin


class HitlForkMixin(CostlyHitlMixin):
    """Mixin: open/answer HITL, idle grace, continue-session fork stub."""

    attempts: dict[str, dict[str, Any]]

    def _resolve_task(self, task_key_or_id: str) -> Task:
        raise NotImplementedError

    def _proposed_check_decision_lock(
        self, proposal_id: str
    ) -> AbstractAsyncContextManager[None]:
        """Per-proposal lock: accept/delegate/reject/timeout/stale serialize side effects."""
        from coscientist.core.plan.proposed_check import hold_proposed_check_decision_lock

        return hold_proposed_check_decision_lock(self, proposal_id)

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

        ``HitlOpened.kind`` stays as requested (``plan`` / ``plan_approval`` / …)
        so GateConfig / UI / report readers keep working. Plan-change offers bind
        ``expand_after_task``; a human edit then produces ``PlanExpanded`` or
        ``ExpansionRefused`` without renaming the gate kind.
        """
        task = self._resolve_task(task_key_or_id)
        assert self.study is not None
        now = self.clock.now()
        # Policy always by the original kind (do NOT rename to plan_expansion).
        after: str | None = expand_after_task
        if kind in ("plan", "plan_approval", "plan_expansion"):
            after = after or task.spec.task_key
        gate = self.study.hitl_policy.gate(kind)
        to = timeout_s if timeout_s is not None else gate.timeout_s
        deadline = min(
            now + timedelta(seconds=to),
            task.deadline,
            self.study.effective_deadline(),
        )
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
        # PR20: proposed_check HITL — CheckApproved / CheckRejected (core decides counts).
        if h.kind == "proposed_check":
            await self._answer_proposed_check(
                h, request_id, decision=decision, expansion=expansion
            )
            return
        # DESIGN §7.3 / PR12 budget gate: «реши сам» → critic; mark HITL node only.
        # Budget shares apply via _apply_budget_hitl (not plan expand).
        if decision == "delegate":
            await self._critic_decide(h, decision="accept", delegated_by="human")
            if h.kind == "budget":
                await self._mark_hitl_decision_node(
                    h, approved_by="critic", delegated_by="human"
                )
                await self._apply_budget_hitl(
                    h, request_id, expansion, approved_by="critic"
                )
            else:
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
            if h.kind == "budget":
                await self._apply_budget_hitl(
                    h, request_id, expansion, approved_by="human"
                )
                await self._mark_hitl_decision_node(
                    h, approved_by="human", delegated_by=None
                )
            else:
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
        from coscientist.core.execution.domain import QuestionSpec, QuestionSupersede, TaskSpec
        from coscientist.core.plan.expand import expand_plan
        from coscientist.core.plan.expand_trigger import refuse_expansion

        after = str(
            expansion.get("after_task")
            or getattr(h, "expand_after_task", None)
            or ""
        )
        if not after and h.task_id:
            task = self.tasks.get(h.task_id)
            after = task.spec.task_key if task is not None else h.task_id
        after = after or "HITL"
        # Plan edits only via plan / plan_approval / plan_expansion (+ expand_after_task).
        # hypotheses / report / … → ExpansionRefused(invalid_hitl), never silent drop.
        if h.kind not in ("plan", "plan_approval", "plan_expansion"):
            await refuse_expansion(
                self,  # type: ignore[arg-type]
                after,
                reason="invalid_hitl",
                detail=f"hitl_wrong_kind:{h.kind or 'unknown'}",
            )
            return
        if not getattr(h, "expand_after_task", None) and not expansion.get("after_task"):
            await refuse_expansion(
                self,  # type: ignore[arg-type]
                after,
                reason="invalid_hitl",
                detail="hitl_unbound",
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

    async def _answer_proposed_check(
        self,
        h: HitlRequest,
        request_id: str,
        *,
        decision: str,
        expansion: dict[str, Any] | None = None,
    ) -> None:
        """HITL gate for proposed_check → core CheckApproved / CheckRejected.

        ``delegate`` schedules a separate reviewer (``delegated=true``); the
        reviewer must call ``approve_check`` / ``reject_check`` with a different
        ``reviewer_attempt_id`` than the proposer (DESIGN_M1 §16).
        """
        from coscientist.core.plan.proposed_check import approve_check, reject_check

        ref_pid = str(getattr(h, "payload_ref", None) or "").strip()
        exp_pid = str((expansion or {}).get("proposal_id") or "").strip()
        if not ref_pid:
            opens = getattr(self, "_hitl_requests", {}) or {}
            row = opens.get(request_id) if isinstance(opens, dict) else None
            if isinstance(row, dict):
                ref_pid = str(
                    row.get("payload_ref") or row.get("proposal_id") or ""
                ).strip()
        # Fail-closed: expansion must not retarget a different proposal than HitlOpened.
        if ref_pid and exp_pid and ref_pid != exp_pid:
            raise ValueError(
                f"proposed_check HITL {request_id!r} payload_ref={ref_pid!r} "
                f"!= proposal_id={exp_pid!r}"
            )
        pid = ref_pid or exp_pid
        if not h.open:
            return
        if not str(pid).strip():
            raise ValueError(
                f"proposed_check HITL {request_id!r} missing proposal_id/payload_ref"
            )

        # Serialize accept/delegate/reject: only the journal winner runs side effects.
        async with self._proposed_check_decision_lock(pid):
            # Refresh open flag under the lock (concurrent answer may have closed it).
            live = self.hitl.get(request_id) if hasattr(self, "hitl") else h
            if live is not None and not getattr(live, "open", True):
                return
            existing = getattr(self, "_hitl_decisions", None) or {}
            if isinstance(existing, dict) and request_id in existing:
                return

            now = self.clock.now()
            decided_idem = make_idem("HitlDecided", request_id)
            if decision in ("accept", "approve"):
                want = "accept"
                payload: dict[str, Any] = {
                    "request_id": request_id,
                    "decision": "accept",
                    "by": "human",
                    "approved_by": "human",
                    "delegated_by": None,
                    "reason": "proposed_check_approved",
                    "proposal_id": pid,
                    "spent_rub": 0.0,
                }
            elif decision == "delegate":
                want = "delegate"
                payload = {
                    "request_id": request_id,
                    "decision": "delegate",
                    "by": "human",
                    "delegated_by": "human",
                    "reason": "delegate→reviewer",
                    "proposal_id": pid,
                    "spent_rub": 0.0,
                    "keep_open": False,
                    "effect": "WAIT_REVIEWER",
                }
            else:
                want = "reject"
                payload = {
                    "request_id": request_id,
                    "decision": "reject",
                    "by": "human",
                    "reason": "rejected",
                    "proposal_id": pid,
                    "spent_rub": 0.0,
                }

            try:
                await self._append(
                    [
                        EventEnvelope(
                            study_id=h.study_id,
                            type="HitlDecided",
                            entity_key=request_id,
                            idem_key=decided_idem,
                            payload=payload,
                            ts=now,
                        )
                    ]
                )
            except Conflict:
                # Idem collapse / loser — do not run side effects.
                return

            # Journal is authority: only the folded winner may approve/reject/schedule.
            folded = (getattr(self, "_hitl_decisions", None) or {}).get(request_id)
            if not isinstance(folded, dict) or str(folded.get("decision") or "") != want:
                return

            if want == "accept":
                await approve_check(
                    self,
                    pid,
                    source="hitl",
                    hitl_request_id_s=request_id,
                )
                return
            if want == "delegate":
                from coscientist.core.plan.proposed_check import schedule_proposed_check_review

                rev_tid = None
                if isinstance(expansion, dict):
                    rev_tid = expansion.get("reviewer_task_id")
                await schedule_proposed_check_review(
                    self,
                    pid,
                    hitl_request_id_s=request_id,
                    reviewer_task_id=str(rev_tid) if rev_tid else None,
                )
                return
            await reject_check(
                self,
                pid,
                source="hitl",
                reason="rejected",
                hitl_request_id_s=request_id,
            )

    async def _hitl_timeout(self, h: HitlRequest) -> None:
        if h.kind == "costly_or_nonreal":
            await self._apply_costly_or_nonreal(h, answer=None)
            return
        # PR20: HITL timeout on proposed_check ≡ reject (StudyFinalized still runs).
        # Must take the same per-proposal lock as answer_hitl — otherwise timeout can
        # write CheckRejected while a human accept is still inside _append.
        if h.kind == "proposed_check":
            from coscientist.core.plan.proposed_check import reject_check

            pid = str(getattr(h, "payload_ref", None) or "").strip()
            if not pid:
                return
            async with self._proposed_check_decision_lock(pid):
                live = self.hitl.get(h.request_id) if hasattr(self, "hitl") else h
                if live is not None and not getattr(live, "open", True):
                    return
                # Journal wins before any side effect.
                existing = getattr(self, "_hitl_decisions", None) or {}
                dec = (
                    existing.get(h.request_id) if isinstance(existing, dict) else None
                )
                if isinstance(dec, dict):
                    d = str(dec.get("decision") or "").strip()
                    decided_by = str(dec["by"]).strip()
                    if decided_by == "human" and d in (
                        "accept",
                        "approve",
                        "delegate",
                        "reject",
                    ):
                        return
                row = (getattr(self, "_check_proposals", None) or {}).get(pid)
                if isinstance(row, dict) and row.get("status") in (
                    "approved",
                    "rejected",
                ):
                    return
                await reject_check(
                    self,
                    pid,
                    source="timeout",
                    reason="human_timeout",
                    hitl_request_id_s=h.request_id,
                )
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
        from coscientist.core.execution.domain import TaskState

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
        # Budget HITL marks the HITL decision node only (not the plan).
        if h.kind not in ("plan", "plan_approval", "plan_expansion", "hypotheses"):
            return
        graph = getattr(self, "graph", None)
        plans = getattr(self, "plans", None)
        if graph is None or not isinstance(plans, dict) or not plans:
            return
        from coscientist.core.claims.graph import GraphNode
        from coscientist.core.plan.supersede import plan_node_id

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

    async def _mark_hitl_decision_node(
        self, h: HitlRequest, *, approved_by: str, delegated_by: str | None
    ) -> None:
        """PR12: budget delegation attrs live on the HITL node only (not plan)."""
        graph = getattr(self, "graph", None)
        if graph is None:
            return
        from coscientist.core.claims.graph import GraphNode

        sid, nid, now = h.study_id, f"{h.study_id}:hitl:{h.request_id}", self.clock.now()
        existing = graph.get_node(nid)
        attrs = dict((existing.attrs if existing is not None else None) or {})
        attrs["approved_by"], attrs["kind_gate"] = approved_by, h.kind
        if delegated_by is not None:
            attrs["delegated_by"] = delegated_by
        node = GraphNode(
            node_id=nid, kind="hitl", study_id=sid, ref_id=h.request_id, attrs=attrs,
            created_at=getattr(existing, "created_at", None) or now,
        )
        await self._append([EventEnvelope(
            study_id=sid, type="GraphNodeUpserted", entity_key=nid,
            idem_key=make_idem(
                "GraphNodeUpserted", nid, "hitl_decision", approved_by, delegated_by or "-"
            ),
            payload=node.model_dump(mode="json"), ts=now, schema_version=1,
        )])

    async def _apply_budget_hitl(
        self,
        h: HitlRequest,
        request_id: str,
        expansion: dict[str, Any] | None,
        *,
        approved_by: str = "human",
    ) -> None:
        """Apply explicit HITL budget shares via journaled BudgetSharesAdjusted.

        Critic rejection emits ``BudgetSharesRefused`` + report line (not silent).
        """
        if not expansion:
            return
        bmap, dmap = expansion.get("budget_shares") or {}, expansion.get("deadline_shares") or {}
        if not bmap and not dmap:
            return
        from coscientist.core.plan.share_alloc import build_budget_shares_payload

        err, payload = build_budget_shares_payload(
            self, budget_shares=dict(bmap),
            deadline_shares=dict(dmap) if dmap else None, request_id=request_id,
        )
        if err is not None or payload is None:
            await self._append(
                [
                    EventEnvelope(
                        study_id=h.study_id,
                        type="BudgetSharesRefused",
                        entity_key=request_id,
                        idem_key=make_idem("BudgetSharesRefused", request_id),
                        payload={
                            "request_id": request_id,
                            "reason": "critic_rejected",
                            "detail": str(err or "invalid"),
                            "approved_by": approved_by,
                        },
                        ts=self.clock.now(),
                        schema_version=1,
                    )
                ]
            )
            return
        payload["approved_by"] = approved_by
        await self._append([EventEnvelope(
            study_id=h.study_id, type="BudgetSharesAdjusted", entity_key=request_id,
            idem_key=make_idem("BudgetSharesAdjusted", request_id),
            payload=payload, ts=self.clock.now(), schema_version=1,
        )])

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
