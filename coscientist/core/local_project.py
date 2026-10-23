"""In-memory projection of M0 state-core events (extracted from machine.py)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from coscientist.core.domain import Study, Task, TaskOutcome, TaskState
from coscientist.core.events import EventEnvelope
from coscientist.core.graph import GraphEdge, GraphNode, GraphStore
from coscientist.core.intake import IntakeRefuse
from coscientist.core.reasons import RefusalKind, TaskReason


class LocalProjectMixin:
    """Fold journal events into StudyRuntime local maps (no HITL)."""

    study: Study | None
    tasks: dict[str, Task]
    attempts: dict[str, dict[str, Any]]
    results: dict[str, dict[str, Any]]
    plans: dict[str, dict[str, Any]]
    graph: GraphStore
    _retry_at: dict[str, datetime]
    _finalized: bool
    _study_cancelled: bool
    _budget_exceeded: bool
    _intake_refusal: IntakeRefuse | None
    _study_spent_rub: Decimal
    verdict_snapshots: dict[str, dict[str, Any]]
    hypothesis_verdicts: dict[str, dict[str, Any]]
    _agent_expansions: int
    _human_expansions: int
    _replan_used_s: float
    _expand_pending: set[str]
    _expand_requests: dict[str, dict[str, Any]]
    _expansion_refusals: list[dict[str, Any]]
    _hitl_decisions: dict[str, dict[str, Any]]
    _hitl_requests: dict[str, dict[str, Any]]
    _hitl_expand_used: set[str]
    _foreach_children: dict[str, list[str]]
    _loop_state: dict[str, dict[str, Any]]
    _loop_capped: dict[str, dict[str, Any]]
    _loop_done: set[str]

    def _clear_expand_request(
        self, *, after_task: str | None = None, plan_key: str | None = None
    ) -> None:
        """Drop PLAN-n pending entries once PlanExpanded / ExpansionRefused folds."""
        reqs = getattr(self, "_expand_requests", None)
        if not isinstance(reqs, dict) or not reqs:
            return
        if plan_key and plan_key in reqs:
            reqs.pop(plan_key, None)
        if after_task:
            for k in [k for k, m in reqs.items() if str(m.get("after_task") or "") == after_task]:
                reqs.pop(k, None)

    def _project_local(self, ev: EventEnvelope) -> None:
        p = ev.payload
        if ev.type == "StudyCreated":
            self.study = Study.model_validate(p)
        elif ev.type == "StudyRefused":
            self._intake_refusal = IntakeRefuse(
                kind=RefusalKind(p["kind"]),
                reason=str(p.get("reason") or ""),
                classifier_error=bool(p.get("classifier_error", False)),
            )
        elif ev.type == "BudgetDebited":
            spent = Decimal(str(p.get("spent_rub") or 0))
            self._study_spent_rub += spent
            tid = p.get("task_id")
            if tid:
                raw_spent = getattr(self, "_task_spent_rub", None)
                if not isinstance(raw_spent, dict):
                    raw_spent = {}
                    self._task_spent_rub: dict[str, Decimal] = raw_spent
                raw_spent[str(tid)] = Decimal(str(raw_spent.get(str(tid), 0))) + spent
        elif ev.type == "BudgetSharesAdjusted":
            from coscientist.core.share_alloc import fold_budget_shares_adjusted

            fold_budget_shares_adjusted(self, p)
        elif ev.type == "BudgetExceeded":
            if str(p.get("scope") or "study") == "study":
                self._budget_exceeded = True
            elif str(p.get("scope") or "") == "task":
                # Task-level exceed is folded via TaskDone(budget); flag unused.
                pass
        elif ev.type == "PlanAccepted":
            self.plans[ev.entity_key] = dict(p)
        elif ev.type == "PlanExpanded":
            self.plans[ev.entity_key] = dict(p)
            # B3: expansion counts + replan_used from events (not live counters).
            # Foreach (PR10) does not spend agent/human expansion quota.
            # Foreach (PR10) / Loop (PR11) do not spend agent/human expansion quota.
            if not p.get("foreach") and not p.get("loop"):
                # timeout → agent quota (DESIGN_M1 §4); only human skips agent quota.
                if p.get("delegated_by") == "human":
                    self._human_expansions = int(getattr(self, "_human_expansions", 0)) + 1
                else:
                    self._agent_expansions = int(getattr(self, "_agent_expansions", 0)) + 1
            alloc = float(p.get("replan_allocated_s") or 0.0)
            if alloc:
                self._replan_used_s = float(getattr(self, "_replan_used_s", 0.0)) + alloc
            after = p.get("after_task")
            pending = getattr(self, "_expand_pending", None)
            if after and isinstance(pending, set):
                pending.discard(str(after))
            # Clear PLAN-n pending rebuilt from TaskCreated (MAJOR replay leak).
            self._clear_expand_request(
                after_task=str(after) if after else None,
                plan_key=p.get("plan_key"),
            )
            rid = p.get("hitl_request_id")
            if rid:
                used = getattr(self, "_hitl_expand_used", None)
                if not isinstance(used, set):
                    self._hitl_expand_used = set()
                    used = self._hitl_expand_used
                used.add(str(rid))
            # PR10: rebuild foreach child index from journal (restart == live).
            if p.get("foreach"):
                tmpl = str(p.get("foreach_template") or "")
                kids = [
                    str(t.get("task_key") if isinstance(t, dict) else getattr(t, "task_key", ""))
                    for t in (p.get("added_tasks") or [])
                ]
                kids = [k for k in kids if k]
                children = getattr(self, "_foreach_children", None)
                if not isinstance(children, dict):
                    self._foreach_children: dict[str, list[str]] = {}
                    children = self._foreach_children
                if tmpl:
                    children[tmpl] = kids
            # PR11: rebuild loop iter index (does not spend expansion quota).
            if p.get("loop"):
                from coscientist.core.loop import fold_loop_plan_expanded

                fold_loop_plan_expanded(self, p)
        elif ev.type == "LoopCapped":
            from coscientist.core.loop import fold_loop_capped

            fold_loop_capped(self, p)
        elif ev.type == "LoopCompleted":
            from coscientist.core.loop import fold_loop_completed

            fold_loop_completed(self, p)
        elif ev.type == "ExpansionRefused":
            refusals = getattr(self, "_expansion_refusals", None)
            if isinstance(refusals, list):
                refusals.append(dict(p))
            else:
                self._expansion_refusals = [dict(p)]
            self._clear_expand_request(
                after_task=str(p.get("after_task") or "") or None,
                plan_key=p.get("plan_key"),
            )
            # PR11: loop refuse must restore capped status on replay.
            if p.get("loop") or p.get("loop_id"):
                from coscientist.core.loop import fold_loop_expansion_refused

                fold_loop_expansion_refused(self, p)
        elif ev.type == "HitlOpened":
            rid = str(p.get("request_id") or ev.entity_key)
            opens = getattr(self, "_hitl_requests", None)
            if not isinstance(opens, dict):
                self._hitl_requests = {}
                opens = self._hitl_requests
            opens[rid] = dict(p)
        elif ev.type == "HitlDecided":
            rid = str(p.get("request_id") or ev.entity_key)
            decisions = getattr(self, "_hitl_decisions", None)
            if not isinstance(decisions, dict):
                self._hitl_decisions = {}
                decisions = self._hitl_decisions
            decisions[rid] = dict(p)
        elif ev.type == "StudyCancelled":
            self._study_cancelled = True
        elif ev.type == "StudyFinalized":
            self._finalized = True
            self._fold_finalized_graph(ev.study_id, p, created_at=ev.ts)
        elif ev.type in ("TaskCreated", "TaskInherited"):
            # Task model forbids extras; strip expansion bookkeeping before validate.
            drop = ("pending_expansion", "expand_after_task", "replan_allocated_s")
            clean = {k: v for k, v in p.items() if k not in drop}
            self.tasks[p["task_id"]] = Task.model_validate(clean)
            # Fold PLAN-n replan_allocated_s (reserve spent creating the container).
            alloc = float(p.get("replan_allocated_s") or 0.0)
            if alloc:
                self._replan_used_s = float(getattr(self, "_replan_used_s", 0.0)) + alloc
            pending_exp = p.get("pending_expansion")
            after = p.get("expand_after_task")
            tkey = str((clean.get("spec") or {}).get("task_key") or "")
            if after and tkey.startswith("PLAN-"):
                reqs = getattr(self, "_expand_requests", None)
                if not isinstance(reqs, dict):
                    self._expand_requests = {}
                    reqs = self._expand_requests
                reqs[tkey] = {
                    "after_task": str(after),
                    "pending": pending_exp if isinstance(pending_exp, dict) else None,
                    "replan_allocated_s": alloc,
                }
                pend = getattr(self, "_expand_pending", None)
                if isinstance(pend, set):
                    pend.add(str(after))
                else:
                    self._expand_pending = {str(after)}
        elif ev.type == "AttemptStarted":
            self.attempts[p["attempt_id"]] = dict(p)
            t = self.tasks[p["task_id"]]
            attempts = list(t.attempts)
            if p["attempt_id"] not in attempts:
                attempts.append(p["attempt_id"])
            upd: dict[str, Any] = {
                "state": TaskState.RUNNING,
                "attempts": attempts,
                "reason": None,
            }
            # PR12: journaled P2 deadline renorm (replay == live).
            if p.get("share_renorm") and p.get("deadline") is not None:
                raw_dl: Any = p["deadline"]
                if isinstance(raw_dl, str):
                    raw_dl = datetime.fromisoformat(raw_dl)
                upd["deadline"] = raw_dl
            self.tasks[p["task_id"]] = t.model_copy(update=upd)
            bud_alloc = p.get("allocated_budget_rub")
            if bud_alloc is not None:
                raw_caps = getattr(self, "_task_budget_alloc", None)
                if not isinstance(raw_caps, dict):
                    raw_caps = {}
                    self._task_budget_alloc: dict[str, Decimal] = raw_caps
                raw_caps[str(p["task_id"])] = Decimal(str(bud_alloc))
            self._retry_at.pop(p["task_id"], None)
        elif ev.type == "AttemptClosed":
            att = self.attempts.get(p["attempt_id"], {})
            att.update(p)
            att["closed"] = True
            self.attempts[p["attempt_id"]] = att
            tid = p.get("task_id") or att.get("task_id")
            if tid and p.get("retry_at"):
                t = self.tasks[tid]
                if t.state != TaskState.DONE:
                    retry_at = p["retry_at"]
                    if isinstance(retry_at, str):
                        retry_at = datetime.fromisoformat(retry_at)
                    self._retry_at[tid] = retry_at
                    self.tasks[tid] = t.model_copy(
                        update={
                            "state": TaskState.PENDING,
                            "reason": TaskReason.RETRY_SCHEDULED,
                        }
                    )
        elif ev.type == "RouteReturned":
            aid = p.get("attempt_id") or ev.entity_key
            att = self.attempts.get(aid, {})
            att = {
                **att,
                "route_returned": True,
                "agent_output_digest": p.get("agent_output_digest"),
            }
            self.attempts[aid] = att
        elif ev.type == "ResultPending":
            # PR13: keep agent_output_digest on the open attempt for judge restart.
            aid = p.get("attempt_id") or ev.entity_key
            dig = p.get("agent_output_digest")
            if aid and dig:
                att = self.attempts.get(aid, {})
                self.attempts[aid] = {**att, "agent_output_digest": dig}
        elif ev.type == "ResultRecorded":
            self.results[p["result_id"]] = dict(p)
            self.results[p["attempt_id"]] = dict(p)
            # Populate dep_outputs from named outputs for InputSpec.bind.
            dep_outputs = getattr(self, "dep_outputs", None)
            dep_provenance = getattr(self, "dep_provenance", None)
            aid = p.get("attempt_id")
            att = self.attempts.get(aid or "", {})
            tid = att.get("task_id") or p.get("task_id")
            if isinstance(dep_outputs, dict) and tid and tid in self.tasks:
                tkey = self.tasks[tid].spec.task_key
                omap = p.get("output_map")
                if isinstance(omap, dict) and omap:
                    dep_outputs[tkey] = {str(k): str(v) for k, v in omap.items() if v}
                elif p.get("artifacts"):
                    arts = list(p.get("artifacts") or [])
                    if arts:
                        dep_outputs.setdefault(tkey, {})
                        dep_outputs[tkey].setdefault("main", str(arts[0]))
                prov = p.get("provenance", "real")
                if isinstance(dep_provenance, dict) and prov in ("real", "partial", "stub"):
                    dep_provenance[tkey] = prov
        elif ev.type == "TaskDone":
            t = self.tasks[p["task_id"]]
            reason = p.get("reason")
            self.tasks[p["task_id"]] = t.model_copy(
                update={
                    "state": TaskState.DONE,
                    "outcome": TaskOutcome(p["outcome"]),
                    "reason": reason,
                }
            )
            self._retry_at.pop(p["task_id"], None)
            # TaskDone(reason=budget) is task-level only; study budget is
            # BudgetExceeded (folded above).
        elif ev.type == "GraphNodeUpserted":
            node = GraphNode.model_validate(p)
            # Never clobber richer existing question/hypothesis attrs with empty.
            existing = self.graph.get_node(node.node_id)
            if (
                existing is not None
                and existing.kind in ("question", "hypothesis")
                and node.kind == existing.kind
                and not (node.attrs or {})
                and (existing.attrs or {})
            ):
                pass  # keep existing
            else:
                # Plan-freeze: stamp frozen_at_seq from journal seq when missing.
                if (
                    node.kind == "criterion"
                    and bool((node.attrs or {}).get("frozen"))
                    and (node.attrs or {}).get("frozen_at_seq") is None
                    and ev.seq is not None
                ):
                    node = node.model_copy(
                        update={
                            "attrs": {
                                **(node.attrs or {}),
                                "frozen_at_seq": int(ev.seq),
                            }
                        }
                    )
                self.graph.upsert_node(node)
            if node.kind == "claim":
                self._invalidate_verdict_snapshots_for_target((node.attrs or {}).get("target"))
        elif ev.type == "GraphEdgeAdded":
            # Fail loudly: missing endpoints / illegal pairs are journal bugs.
            edge = GraphEdge.model_validate(p)
            self.graph.add_edge(edge)
            if edge.kind == "about":
                # Scope invalidation to the about target (not all snapshots).
                dst = self.graph.get_node(edge.dst_id)
                logical = None
                if dst is not None:
                    prefix = f"{ev.study_id}:{dst.kind}:"
                    logical = (
                        edge.dst_id[len(prefix) :] if edge.dst_id.startswith(prefix) else (dst.ref_id or edge.dst_id)
                    )
                self._invalidate_verdict_snapshots_for_target(logical)
        elif ev.type == "VerdictSnapshotsRecorded":
            snaps = p.get("snapshots") or {}
            if isinstance(snaps, dict):
                self.verdict_snapshots.update({str(k): dict(v) for k, v in snaps.items() if isinstance(v, dict)})

    def _invalidate_verdict_snapshots_for_target(self, target: Any) -> None:
        """Drop journaled snapshots for one target ref (DESIGN_M1 PR8)."""
        if target is None:
            return
        key = str(target)
        self.verdict_snapshots.pop(key, None)
        # Kind-prefixed keys (question:Q1 / hypothesis:H1) if present.
        if ":" not in key or key.split(":", 1)[0] not in ("question", "hypothesis"):
            self.verdict_snapshots.pop(f"question:{key}", None)
            self.verdict_snapshots.pop(f"hypothesis:{key}", None)

    def _fold_finalized_graph(self, study_id: str, payload: dict[str, Any], *, created_at: datetime) -> None:
        from coscientist.core.finalize_nodes import graph_rows_from_finalized

        nodes, edges = graph_rows_from_finalized(study_id, payload, created_at=created_at)
        for row in nodes.values():
            incoming = GraphNode.model_validate(row)
            existing = self.graph.get_node(incoming.node_id)
            if (
                existing is not None
                and existing.kind in ("question", "hypothesis")
                and (existing.attrs or {})
                and not (incoming.attrs or {}).get("formulation")
            ):
                # Preserve formulation from accept_plan when finalize row is sparse.
                merged = incoming.model_copy(update={"attrs": {**(existing.attrs or {}), **(incoming.attrs or {})}})
                self.graph.upsert_node(merged)
            else:
                self.graph.upsert_node(incoming)
        for row in edges.values():
            try:
                self.graph.add_edge(GraphEdge.model_validate(row))
            except (KeyError, ValueError):
                # study/question node may be missing in sparse journals
                pass
        # Keep hypothesis_verdicts separate from bind's verdict_snapshots cache.
        hv = payload.get("hypothesis_verdicts")
        if isinstance(hv, dict):
            self.hypothesis_verdicts.update({str(k): dict(v) for k, v in hv.items() if isinstance(v, dict)})
