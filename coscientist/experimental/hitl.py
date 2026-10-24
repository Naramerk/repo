"""HITL + fork extensions (experimental / pre-M2)."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any, Self

from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import Conflict, JournalFormatError
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.ids import hitl_request_id, study_id
from coscientist.core.events.schema_versions import GRAPH_NODE_UPSERTED_SCHEMA_VERSION
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.domain import Clarification, HitlRequest, Task
from coscientist.experimental.hitl_costly import CostlyHitlMixin

_BUDGET_SHARES_REFUSED_SV = writer_schema_version("BudgetSharesRefused")
_BUDGET_SHARES_ADJUSTED_SV = writer_schema_version("BudgetSharesAdjusted")


class HitlForkMixin(CostlyHitlMixin):
    """Mixin: open/answer HITL, idle grace, continue-session fork stub."""

    attempts: dict[str, dict[str, Any]]
    graph: Any
    plans: dict[str, dict[str, Any]]
    # Folded on StudyRuntime; required dicts (no getattr→{} invention).
    _hitl_decisions: dict[str, Any]
    _check_proposals: dict[str, Any]

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
        kind: str,
        timeout_s: int | None = None,
        *,
        cost_rub: float,
        expand_after_task: str | None = None,
    ) -> str:
        """Open a HITL gate.

        ``kind`` and ``cost_rub`` are required (no invented defaults).
        ``HitlOpened.kind`` stays as requested (``plan`` / ``plan_approval`` / …)
        so GateConfig / UI / report readers keep working. Plan-change offers bind
        ``expand_after_task``; a human edit then produces ``PlanExpanded`` or
        ``ExpansionRefused`` without renaming the gate kind.
        """
        task = self._resolve_task(task_key_or_id)
        if self.study is None:
            raise RuntimeError("open_hitl requires runtime.study")
        now = self.clock.now()
        # Policy always by the original kind (do NOT rename to plan_expansion).
        after: str | None = expand_after_task
        if kind in ("plan", "plan_approval", "plan_expansion"):
            if after is None:
                raise ValueError(
                    f"expand_after_task required for HITL kind {kind!r} "
                    "(no fallback to task_key)"
                )
        gate = self.study.hitl_policy.gate(kind)
        if gate is None:
            raise ValueError(f"HITL kind {kind!r} has no gate in hitl_policy")
        to = timeout_s if timeout_s is not None else gate.timeout_s
        deadline = min(
            now + timedelta(seconds=to),
            task.deadline,
            self.study.effective_deadline(),
        )
        rid = hitl_request_id()
        if type(cost_rub) not in (int, float) or isinstance(cost_rub, bool):
            raise TypeError(f"cost_rub must be int|float, got {type(cost_rub).__name__}")
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
                UnstampedEvent(
                    study_id=self.study.study_id,
                    type="HitlOpened",
                    entity_key=rid,
                    idem_key=make_idem("HitlOpened", rid),
                    payload=req.model_dump(mode="json"),
                    ts=now,
                    schema_version=writer_schema_version('HitlOpened'),
                )
            ]
        )
        return rid

    async def answer_hitl(
        self,
        request_id: str,
        decision: str,
        *,
        critic_accepts: bool,
        expansion: dict[str, Any] | None = None,
    ) -> None:
        h = self.hitl[request_id]
        if not h.open:
            return
        if h.kind == "costly_or_nonreal":
            # Costly path wire tokens: approve|reject|delegate only (no accept alias).
            if decision not in ("approve", "reject", "delegate"):
                raise ValueError(f"unknown costly HITL decision: {decision!r}")
            await self._apply_costly_or_nonreal(
                h, answer=decision, critic_accepts=critic_accepts  # type: ignore[arg-type]
            )
            return
        # Canonical decision tokens for non-costly gates (no accept↔approve alias).
        if decision not in ("accept", "reject", "delegate"):
            raise ValueError(f"unknown HITL decision: {decision!r}")
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
                UnstampedEvent(
                    study_id=h.study_id,
                    type="HitlDecided",
                    entity_key=request_id,
                    idem_key=make_idem("HitlDecided", request_id),
                    payload={
                        "request_id": request_id,
                        "decision": decision,
                        "by": "human",
                        "delegated_by": "human",
                        "reason": "answered",
                        "spent_rub": 0.0,
                    },
                    ts=now,
                    schema_version=writer_schema_version("HitlDecided"),
                )
            ]
        )
        if decision == "accept":
            if h.kind == "budget":
                await self._apply_budget_hitl(
                    h, request_id, expansion, approved_by="human"
                )
                await self._mark_hitl_decision_node(
                    h, approved_by="human", delegated_by="human"
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

        # Sole bind: expansion.after_task or HitlOpened.expand_after_task (no task_id/"HITL").
        raw_after = expansion.get("after_task")
        if raw_after is None:
            raw_after = h.expand_after_task
        if not isinstance(raw_after, str) or not raw_after:
            if not isinstance(h.task_id, str) or not h.task_id:
                raise JournalFormatError(
                    f"HITL {request_id!r} unbound and missing task_id"
                )
            await refuse_expansion(
                self,  # type: ignore[arg-type]
                h.task_id,
                reason="invalid_hitl",
                detail="hitl_unbound",
            )
            return
        after = raw_after
        # Plan edits only via plan / plan_approval / plan_expansion (+ expand_after_task).
        # hypotheses / report / … → ExpansionRefused(invalid_hitl), never silent drop.
        if h.kind not in ("plan", "plan_approval", "plan_expansion"):
            await refuse_expansion(
                self,  # type: ignore[arg-type]
                after,
                reason="invalid_hitl",
                detail=f"hitl_wrong_kind:{h.kind}",
            )
            return
        if "added_tasks" not in expansion:
            await refuse_expansion(
                self,  # type: ignore[arg-type]
                after,
                reason="invalid_hitl",
                detail="hitl_missing_added_tasks",
            )
            return
        raw_tasks = expansion["added_tasks"]
        if not isinstance(raw_tasks, list):
            await refuse_expansion(
                self,  # type: ignore[arg-type]
                after,
                reason="invalid_hitl",
                detail="hitl_bad_added_tasks",
            )
            return
        added_tasks = [
            TaskSpec.model_validate(t) if isinstance(t, dict) else t for t in raw_tasks
        ]
        if "added_questions" in expansion:
            raw_qs = expansion["added_questions"]
        else:
            raw_qs = []
        if not isinstance(raw_qs, list):
            await refuse_expansion(
                self,  # type: ignore[arg-type]
                after,
                reason="invalid_hitl",
                detail="hitl_bad_added_questions",
            )
            return
        added_questions = [
            QuestionSpec.model_validate(q) if isinstance(q, dict) else q for q in raw_qs
        ]
        if "superseded_questions" in expansion:
            raw_sq = expansion["superseded_questions"]
        else:
            raw_sq = []
        if not isinstance(raw_sq, list):
            await refuse_expansion(
                self,  # type: ignore[arg-type]
                after,
                reason="invalid_hitl",
                detail="hitl_bad_superseded_questions",
            )
            return
        superseded_questions = [
            QuestionSupersede.model_validate(s) if isinstance(s, dict) else s
            for s in raw_sq
        ]
        if "superseded_tasks" in expansion:
            raw_st = expansion["superseded_tasks"]
        else:
            raw_st = []
        if not isinstance(raw_st, list):
            await refuse_expansion(
                self,  # type: ignore[arg-type]
                after,
                reason="invalid_hitl",
                detail="hitl_bad_superseded_tasks",
            )
            return
        if "reason" not in expansion:
            raise ValueError("HITL plan expansion requires expansion['reason']")
        reason = expansion["reason"]
        if not isinstance(reason, str) or not reason:
            raise ValueError("HITL plan expansion reason must be non-empty str")
        if "assign_keys" not in expansion:
            assign_keys = False
        else:
            raw_ak = expansion["assign_keys"]
            if not isinstance(raw_ak, bool):
                raise ValueError(
                    f"HITL expansion assign_keys must be bool, got {type(raw_ak).__name__}"
                )
            assign_keys = raw_ak
        st_keys: list[str] = []
        for x in raw_st:
            if not isinstance(x, str) or not x:
                raise ValueError(f"HITL superseded_tasks entry must be non-empty str: {x!r}")
            st_keys.append(x)
        await expand_plan(
            self,  # type: ignore[arg-type]
            after_task=after,
            added_tasks=added_tasks,
            added_questions=added_questions,
            superseded_tasks=st_keys,
            superseded_questions=superseded_questions,
            origin="plan",
            reason=reason,
            hitl_request_id=request_id,
            assign_keys=assign_keys,
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

        # Sole bind: live HitlRequest.payload_ref (no _hitl_requests / expansion fallback).
        raw_ref = h.payload_ref
        if isinstance(raw_ref, str):
            ref_pid = raw_ref.strip()
        else:
            ref_pid = ""
        exp_pid = ""
        if expansion is not None and "proposal_id" in expansion:
            raw_exp = expansion["proposal_id"]
            if not isinstance(raw_exp, str):
                raise ValueError(
                    f"proposed_check HITL {request_id!r} proposal_id must be str"
                )
            exp_pid = raw_exp.strip()
        # Fail-closed: expansion must not retarget a different proposal than HitlOpened.
        if ref_pid and exp_pid and ref_pid != exp_pid:
            raise ValueError(
                f"proposed_check HITL {request_id!r} payload_ref={ref_pid!r} "
                f"!= proposal_id={exp_pid!r}"
            )
        pid = ref_pid if ref_pid else exp_pid
        if not h.open:
            return
        if not pid:
            raise ValueError(
                f"proposed_check HITL {request_id!r} missing proposal_id/payload_ref"
            )

        # Serialize accept/delegate/reject: only the journal winner runs side effects.
        async with self._proposed_check_decision_lock(pid):
            # Refresh open flag under the lock (concurrent answer may have closed it).
            live = self.hitl[request_id]
            if not live.open:
                return
            existing = self._hitl_decisions
            if not isinstance(existing, dict):
                raise JournalFormatError(
                    f"_hitl_decisions must be dict, got {type(existing).__name__}"
                )
            if request_id in existing:
                return

            now = self.clock.now()
            decided_idem = make_idem("HitlDecided", request_id)
            # Canonical proposed_check decisions: accept | delegate | reject (no approve alias).
            if decision == "accept":
                want = "accept"
                payload: dict[str, Any] = {
                    "request_id": request_id,
                    "decision": "accept",
                    "by": "human",
                    "delegated_by": "human",
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
            elif decision == "reject":
                want = "reject"
                payload = {
                    "request_id": request_id,
                    "decision": "reject",
                    "by": "human",
                    "reason": "rejected",
                    "proposal_id": pid,
                    "spent_rub": 0.0,
                }
            else:
                raise ValueError(
                    f"proposed_check HITL decision must be accept|delegate|reject, got {decision!r}"
                )

            try:
                await self._append(
                    [
                        UnstampedEvent(
                            study_id=h.study_id,
                            type="HitlDecided",
                            entity_key=request_id,
                            idem_key=decided_idem,
                            payload=payload,
                            ts=now,
                            schema_version=writer_schema_version('HitlDecided'),
                        )
                    ]
                )
            except Conflict:
                # Idem collapse / loser — do not run side effects.
                return

            # Journal is authority: only the folded winner may approve/reject/schedule.
            decisions = self._hitl_decisions
            if not isinstance(decisions, dict):
                raise JournalFormatError("_hitl_decisions missing after HitlDecided")
            folded = decisions.get(request_id)
            if not isinstance(folded, dict):
                raise JournalFormatError(
                    f"HitlDecided fold for {request_id!r} missing or not dict"
                )
            if "decision" not in folded:
                raise JournalFormatError("HitlDecided fold missing 'decision'")
            folded_decision = folded["decision"]
            if not isinstance(folded_decision, str):
                raise JournalFormatError(
                    f"HitlDecided decision must be str, got {type(folded_decision).__name__}"
                )
            if folded_decision != want:
                return

            if want == "accept":
                await approve_check(
                    self,
                    pid,
                    source="hitl",
                    hitl_request_id_s=request_id, delegated=False)
                return
            if want == "delegate":
                from coscientist.core.plan.proposed_check import schedule_proposed_check_review

                rev_tid = None
                if isinstance(expansion, dict) and "reviewer_task_id" in expansion:
                    raw_rev = expansion["reviewer_task_id"]
                    if raw_rev is not None and not isinstance(raw_rev, str):
                        raise ValueError(
                            f"reviewer_task_id must be str|None, got {type(raw_rev).__name__}"
                        )
                    rev_tid = raw_rev
                await schedule_proposed_check_review(
                    self,
                    pid,
                    hitl_request_id_s=request_id,
                    reviewer_task_id=rev_tid,
                )
                return
            await reject_check(
                self,
                pid,
                source="hitl",
                reason="rejected",
                hitl_request_id_s=request_id, delegated=False)

    async def _hitl_timeout(self, h: HitlRequest) -> None:
        if h.kind == "costly_or_nonreal":
            await self._apply_costly_or_nonreal(h, answer=None, critic_accepts=True)
            return
        # PR20: HITL timeout on proposed_check ≡ reject (StudyFinalized still runs).
        # Must take the same per-proposal lock as answer_hitl — otherwise timeout can
        # write CheckRejected while a human accept is still inside _append.
        if h.kind == "proposed_check":
            from coscientist.core.plan.proposed_check import reject_check

            if not isinstance(h.payload_ref, str) or not h.payload_ref.strip():
                raise JournalFormatError(
                    f"proposed_check HITL {h.request_id!r} missing payload_ref"
                )
            pid = h.payload_ref.strip()
            async with self._proposed_check_decision_lock(pid):
                live = self.hitl[h.request_id]
                if not live.open:
                    return
                # Journal wins before any side effect.
                existing = self._hitl_decisions
                if not isinstance(existing, dict):
                    raise JournalFormatError(
                        f"_hitl_decisions must be dict, got {type(existing).__name__}"
                    )
                dec = existing.get(h.request_id)
                if isinstance(dec, dict):
                    if "decision" not in dec or "by" not in dec:
                        raise JournalFormatError(
                            f"HitlDecided for {h.request_id!r} missing decision/by"
                        )
                    raw_d = dec["decision"]
                    raw_by = dec["by"]
                    if not isinstance(raw_d, str) or not isinstance(raw_by, str):
                        raise JournalFormatError(
                            f"HitlDecided for {h.request_id!r} decision/by must be str"
                        )
                    d = raw_d.strip()
                    decided_by = raw_by.strip()
                    if decided_by == "human" and d in (
                        "accept",
                        "delegate",
                        "reject",
                    ):
                        return
                proposals = self._check_proposals
                if not isinstance(proposals, dict):
                    raise JournalFormatError(
                        f"_check_proposals must be dict, got {type(proposals).__name__}"
                    )
                row = proposals.get(pid)
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
                    hitl_request_id_s=h.request_id, delegated=False)
            return
        now = self.clock.now()
        await self._append(
            [
                UnstampedEvent(
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
                    schema_version=writer_schema_version('HitlTimedOut'),
                )
            ]
        )
        # Fail-closed: missing study/policy/gate → refuse (never invent on_timeout=delegate).
        if self.study is None or self.study.hitl_policy is None:
            task = self.tasks[h.task_id]
            from coscientist.core.execution.domain import TaskState

            if task.state != TaskState.DONE:
                await self._fail_task_human_timeout(h, task)
            return
        policy = self.study.hitl_policy
        gate = policy.gate(h.kind)
        if gate is None:
            task = self.tasks[h.task_id]
            from coscientist.core.execution.domain import TaskState

            if task.state != TaskState.DONE:
                await self._fail_task_human_timeout(h, task)
            return
        on_timeout = gate.on_timeout
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
                UnstampedEvent(
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
                    schema_version=writer_schema_version('HitlDecided'),
                )
            ]
        )
        # Mark plan node when «decide yourself» / timeout delegate (graph attrs).
        await self._mark_plan_delegated(h, delegated_by=delegated_by)

    async def _mark_plan_delegated(self, h: HitlRequest, *, delegated_by: str) -> None:
        # Budget HITL marks the HITL decision node only (not the plan).
        if h.kind not in ("plan", "plan_approval", "plan_expansion", "hypotheses"):
            return
        try:
            graph = self.graph
            plans = self.plans
        except AttributeError as e:
            raise JournalFormatError("runtime missing graph/plans for HITL delegate mark") from e
        if graph is None or not isinstance(plans, dict) or not plans:
            return
        from coscientist.core.claims.graph import GraphNode
        from coscientist.core.events.upcasters import require_exact_int
        from coscientist.core.plan.supersede import plan_node_id

        def _rev(p: dict[str, Any]) -> int:
            if "revision" not in p:
                raise JournalFormatError("plan blob missing revision")
            return require_exact_int(p["revision"], what="plan.revision")

        prev = max(plans.values(), key=_rev)
        if "plan_id" not in prev:
            raise JournalFormatError("plan blob missing plan_id")
        pid = prev["plan_id"]
        if not isinstance(pid, str) or not pid:
            raise JournalFormatError("plan.plan_id must be non-empty str")
        sid = h.study_id
        pnid = plan_node_id(sid, pid)
        existing = graph.get_node(pnid)
        if existing is not None:
            attrs = dict(existing.attrs)
            created_at = existing.created_at
        else:
            attrs = {}
            created_at = None
        attrs["delegated_by"] = delegated_by
        attrs["approved_by"] = "critic"
        now = self.clock.now()
        node = GraphNode(
            node_id=pnid,
            kind="plan",
            study_id=sid,
            ref_id=pid,
            attrs=attrs,
            created_at=created_at if created_at is not None else now,
        )
        await self._append(
            [
                UnstampedEvent(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=pnid,
                    idem_key=make_idem("GraphNodeUpserted", pnid, "hitl_delegate", delegated_by),
                    payload=node.model_dump(mode="json"),
                    ts=now,
                    schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
                )
            ]
        )

    async def _mark_hitl_decision_node(
        self, h: HitlRequest, *, approved_by: str, delegated_by: str | None
    ) -> None:
        """PR12: budget delegation attrs live on the HITL node only (not plan)."""
        try:
            graph = self.graph
        except AttributeError as e:
            raise JournalFormatError("runtime missing graph for HITL decision mark") from e
        if graph is None:
            return
        from coscientist.core.claims.graph import GraphNode

        sid, nid, now = h.study_id, f"{h.study_id}:hitl:{h.request_id}", self.clock.now()
        existing = graph.get_node(nid)
        if existing is not None:
            attrs = dict(existing.attrs)
            created_at = existing.created_at
        else:
            attrs = {}
            created_at = None
        attrs["approved_by"], attrs["kind_gate"] = approved_by, h.kind
        if delegated_by is not None:
            attrs["delegated_by"] = delegated_by
        node = GraphNode(
            node_id=nid, kind="hitl", study_id=sid, ref_id=h.request_id, attrs=attrs,
            created_at=created_at if created_at is not None else now,
        )
        if delegated_by is None:
            del_token = "-"
        else:
            del_token = delegated_by
        await self._append([UnstampedEvent(
            study_id=sid, type="GraphNodeUpserted", entity_key=nid,
            idem_key=make_idem(
                "GraphNodeUpserted", nid, "hitl_decision", approved_by, del_token
            ),
            payload=node.model_dump(mode="json"),
            ts=now,
            schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
        )])

    async def _apply_budget_hitl(
        self,
        h: HitlRequest,
        request_id: str,
        expansion: dict[str, Any] | None,
        *,
        approved_by: str,
    ) -> None:
        """Apply explicit HITL budget shares via journaled BudgetSharesAdjusted.

        Critic rejection emits ``BudgetSharesRefused`` + report line (not silent).
        ``approved_by`` is required (no invented ``human`` default).
        """
        if not expansion:
            return
        if "budget_shares" in expansion:
            bmap = expansion["budget_shares"]
            if bmap is None:
                raise ValueError("budget_shares must be dict, got None")
        else:
            bmap = {}
        if "deadline_shares" in expansion:
            dmap = expansion["deadline_shares"]
            if dmap is None:
                raise ValueError("deadline_shares must be dict, got None")
        else:
            dmap = {}
        if not bmap and not dmap:
            return
        if not isinstance(bmap, dict) or not isinstance(dmap, dict):
            raise ValueError("budget_shares/deadline_shares must be dict")
        from coscientist.core.plan.share_alloc import build_budget_shares_payload

        err, payload = build_budget_shares_payload(
            self, budget_shares=dict(bmap),
            deadline_shares=dict(dmap) if dmap else None, request_id=request_id,
        )
        if err is not None or payload is None:
            if err is not None:
                detail = err
            else:
                detail = "invalid"
            await self._append(
                [
                    UnstampedEvent(
                        study_id=h.study_id,
                        type="BudgetSharesRefused",
                        entity_key=request_id,
                        idem_key=make_idem("BudgetSharesRefused", request_id),
                        payload={
                            "request_id": request_id,
                            "reason": "critic_rejected",
                            "detail": detail,
                            "approved_by": approved_by,
                        },
                        ts=self.clock.now(),
                        schema_version=_BUDGET_SHARES_REFUSED_SV,
                    )
                ]
            )
            return
        payload["approved_by"] = approved_by
        await self._append([UnstampedEvent(
            study_id=h.study_id, type="BudgetSharesAdjusted", entity_key=request_id,
            idem_key=make_idem("BudgetSharesAdjusted", request_id),
            payload=payload,
            ts=self.clock.now(),
            schema_version=_BUDGET_SHARES_ADJUSTED_SV,
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
                    UnstampedEvent(
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
                        schema_version=writer_schema_version('HitlDeadlineExtended'),
                    )
                ]
            )

    async def fork(
        self, clarifications: list[Clarification], *, nonce: str
    ) -> Self:
        """M0 stub: compute affected subgraph; full fork lands in M2.

        ``nonce`` is required (may be empty str only when caller passes it explicitly).
        """
        if self.study is None:
            raise RuntimeError("fork requires runtime.study")
        if not isinstance(nonce, str):
            raise TypeError(f"fork nonce must be str, got {type(nonce).__name__}")
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
