"""In-memory projection of M0 state-core events (extracted from machine.py)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from coscientist.core.claims.graph import GraphEdge, GraphNode, GraphStore
from coscientist.core.claims.reasons import RefusalKind, TaskReason, coerce_task_reason
from coscientist.core.events.events import EventEnvelope
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import (
    PROVENANCE_ORDER,
    Provenance,
    Study,
    Task,
    TaskOutcome,
    TaskState,
)
from coscientist.core.execution.executor import Executor
from coscientist.core.plan.intake import IntakeRefuse

log = logging.getLogger(__name__)


class LocalProjectMixin:
    """Fold journal events into StudyRuntime local maps (no HITL)."""

    # Every attribute below is assigned by StudyRuntime.__init__ before any
    # event fold runs; fold methods read them directly (no defensive lookups).
    study: Study | None
    tasks: dict[str, Task]
    attempts: dict[str, dict[str, Any]]
    results: dict[str, dict[str, Any]]
    plans: dict[str, dict[str, Any]]
    graph: GraphStore
    executor: Executor | None
    artifacts: LocalArtifactStore | None
    dep_outputs: dict[str, dict[str, str]]
    dep_provenance: dict[str, Provenance]
    max_attempts: int
    _retry_at: dict[str, datetime]
    _finalized: bool
    _study_cancelled: bool
    _budget_exceeded: bool
    _intake_refusal: IntakeRefuse | None
    _study_spent_rub: Decimal
    _task_budget_alloc: dict[str, Decimal]
    _task_spent_rub: dict[str, Decimal]
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
    question_disposals: dict[str, dict[str, Any]]
    artifact_kinds: dict[str, str]
    _disposal_rejects: list[dict[str, Any]]
    _stamp_meta_missing: list[dict[str, Any]]
    _duplicate_study_finalized: list[dict[str, Any]]
    _pending_kind_stamps: list[tuple[str, str]]

    def _apply_kind_stamp_soft(self, artifact_id: str, kind: str) -> bool:
        """Apply journaled kind; missing meta → tainted/internal_error (never abort open).

        Returns True only when meta was stamped. Callers must not record
        ``artifact_kinds[aid]`` on failure — otherwise ``design_artifact_ok``
        would unlock ``needs_more_data`` without a real design artifact.
        """
        arts = self.artifacts
        if arts is None:
            return False
        from coscientist.core.events.errors import ArtifactRejected
        from coscientist.core.execution.artifact_stamp import apply_kind_stamp

        kinds = self.artifact_kinds
        try:
            apply_kind_stamp(arts, artifact_id, kind)
        except ArtifactRejected:
            if isinstance(kinds, dict):
                kinds.pop(artifact_id, None)
            self._stamp_meta_missing.append(
                {
                    "artifact_id": artifact_id,
                    "kind": kind,
                    "tainted": True,
                    "internal_error": True,
                }
            )
            return False
        return True

    def _flush_pending_kind_stamps(self) -> None:
        """Apply stamps deferred because fold ran before artifacts were bound."""
        pending = self._pending_kind_stamps
        if not pending:
            return
        self._pending_kind_stamps = []
        for art_id, kind in pending:
            if self._apply_kind_stamp_soft(art_id, kind):
                self.artifact_kinds[art_id] = kind

    def _clear_expand_request(self, *, after_task: str | None = None, plan_key: str | None = None) -> None:
        """Drop PLAN-n pending entries once PlanExpanded / ExpansionRefused folds."""
        reqs = self._expand_requests
        if not reqs:
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
            # RC17a: Study owns retry cap — sync runtime + bound Executor (no invent-3).
            self.max_attempts = int(self.study.max_attempts)
            ex = self.executor
            if ex is not None:
                ex.max_attempts = self.max_attempts
        elif ev.type == "StudyRefused":
            # v2 writer / post-upcast payload must carry catalog_version (nullable).
            if "catalog_version" not in p:
                from coscientist.core.events.errors import CatalogError

                raise CatalogError("StudyRefused.v2:catalog_version_required")
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
                raw_spent = self._task_spent_rub
                raw_spent[str(tid)] = Decimal(str(raw_spent.get(str(tid), 0))) + spent
        elif ev.type == "BudgetSharesAdjusted":
            from coscientist.core.plan.share_alloc import fold_budget_shares_adjusted

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
                    self._human_expansions += 1
                else:
                    self._agent_expansions += 1
            alloc = float(p.get("replan_allocated_s") or 0.0)
            if alloc:
                self._replan_used_s += alloc
            after = p.get("after_task")
            if after:
                self._expand_pending.discard(str(after))
            # Clear PLAN-n pending rebuilt from TaskCreated (MAJOR replay leak).
            self._clear_expand_request(
                after_task=str(after) if after else None,
                plan_key=p.get("plan_key"),
            )
            rid = p.get("hitl_request_id")
            if rid:
                self._hitl_expand_used.add(str(rid))
            # PR10: rebuild foreach child index from journal (restart == live).
            if p.get("foreach"):
                tmpl = str(p.get("foreach_template") or "")
                kids = [
                    str(t.get("task_key") if isinstance(t, dict) else getattr(t, "task_key", ""))
                    for t in (p.get("added_tasks") or [])
                ]
                kids = [k for k in kids if k]
                if tmpl:
                    self._foreach_children[tmpl] = kids
            # PR11: rebuild loop iter index (does not spend expansion quota).
            if p.get("loop"):
                from coscientist.core.plan.loop import fold_loop_plan_expanded

                fold_loop_plan_expanded(self, p)
        elif ev.type == "LoopCapped":
            from coscientist.core.plan.loop import fold_loop_capped

            fold_loop_capped(self, p)
        elif ev.type == "LoopRefused":
            from coscientist.core.plan.loop import fold_loop_refused

            fold_loop_refused(self, p)
        elif ev.type == "LoopCompleted":
            from coscientist.core.plan.loop import fold_loop_completed

            fold_loop_completed(self, p)
        elif ev.type == "ExpansionRefused":
            self._expansion_refusals.append(dict(p))
            self._clear_expand_request(
                after_task=str(p.get("after_task") or "") or None,
                plan_key=p.get("plan_key"),
            )
            # PR11: loop refuse must restore status on replay.
            if p.get("loop") or p.get("loop_id"):
                from coscientist.core.plan.loop import fold_loop_expansion_refused

                fold_loop_expansion_refused(self, p)
        elif ev.type == "HitlOpened":
            rid = str(p.get("request_id") or ev.entity_key)
            self._hitl_requests[rid] = dict(p)
        elif ev.type == "HitlDecided":
            rid = str(p.get("request_id") or ev.entity_key)
            self._hitl_decisions[rid] = dict(p)
            # Close the open gate unless keep_open / WAIT. Do not merge decision
            # payload fields into the HitlOpened row (request ≠ decision).
            # RC7r: plan-gate reject stamps status=rejected on the open-request row.
            opens = self._hitl_requests
            if rid in opens:
                keep_open = bool(p.get("keep_open") or p.get("effect") == "WAIT")
                row = {**opens[rid], "open": keep_open}
                raw_decision = p.get("decision")
                if type(raw_decision) is str:
                    from coscientist.core.plan.need import stamp_hitl_reject_status

                    row = stamp_hitl_reject_status(row, decision=raw_decision)
                opens[rid] = row
        elif ev.type in (
            "CheckProposed",
            "CheckApproved",
            "CheckRejected",
            "ReviewScheduled",
        ):
            from coscientist.core.plan.proposed_check import fold_proposed_check_event

            fold_proposed_check_event(self, ev)
        elif ev.type == "StudyCancelled":
            self._study_cancelled = True
        elif ev.type == "ArtifactRegistered":
            # PR15: core-stamped Artifact.kind (experiment_design / contradiction_map).
            # Journal is authority; meta mutation only here (after successful append).
            art_id = str(p.get("artifact_id") or ev.entity_key or "")
            kind = p.get("kind")
            if art_id and kind and bool(p.get("stamped")):
                if self.artifacts is None:
                    # open_runtime folds before make_executor — defer stamp apply.
                    # Do not set artifact_kinds until soft stamp succeeds (flush).
                    self._pending_kind_stamps.append((art_id, str(kind)))
                elif self._apply_kind_stamp_soft(art_id, str(kind)):
                    self.artifact_kinds[art_id] = str(kind)
        elif ev.type == "QuestionDisposed":
            from coscientist.core.claims.dispose import fold_question_disposed

            store = self.question_disposals
            fold_question_disposed(store, p if isinstance(p, dict) else {})
        elif ev.type == "QuestionDisposeRejected":
            from coscientist.core.claims.dispose import fold_question_dispose_rejected

            notes = self._disposal_rejects
            fold_question_dispose_rejected(notes, p if isinstance(p, dict) else {})
        elif ev.type == "StudyFinalized":
            # First-wins: ignore duplicate StudyFinalized on replay (legacy journals).
            # Record a taint mark so reopen/report can surface the I3 breach.
            if self._finalized:
                log.warning(
                    "ignoring duplicate StudyFinalized study_id=%s seq=%s (first-wins; local fold skipped; tainted)",
                    ev.study_id,
                    ev.seq,
                )
                dups = self._duplicate_study_finalized
                dups.append(
                    {
                        "study_id": ev.study_id,
                        "seq": ev.seq,
                        "tainted": True,
                    }
                )
            else:
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
                self._replan_used_s += alloc
            pending_exp = p.get("pending_expansion")
            after = p.get("expand_after_task")
            tkey = str((clean.get("spec") or {}).get("task_key") or "")
            if after and tkey.startswith("PLAN-"):
                self._expand_requests[tkey] = {
                    "after_task": str(after),
                    "pending": pending_exp if isinstance(pending_exp, dict) else None,
                    "replan_allocated_s": alloc,
                }
                self._expand_pending.add(str(after))
        elif ev.type == "AttemptStarted":
            row = dict(p)
            row.setdefault("attempt_role", "agent")
            if ev.seq is not None:
                row["_seq"] = ev.seq
            self.attempts[p["attempt_id"]] = row
            t = self.tasks[p["task_id"]]
            attempts = list(t.attempts)
            if p["attempt_id"] not in attempts:
                attempts.append(p["attempt_id"])
            upd: dict[str, object] = {
                "state": TaskState.RUNNING,
                "attempts": attempts,
                "reason": None,
            }
            # PR12: journaled P2 deadline renorm (replay == live).
            if p.get("share_renorm") and p.get("deadline") is not None:
                raw_dl: object = p["deadline"]
                if isinstance(raw_dl, str):
                    raw_dl = datetime.fromisoformat(raw_dl)
                upd["deadline"] = raw_dl
            self.tasks[p["task_id"]] = t.model_copy(update=upd)
            bud_alloc = p.get("allocated_budget_rub")
            if bud_alloc is not None:
                self._task_budget_alloc[str(p["task_id"])] = Decimal(str(bud_alloc))
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
            row = dict(p)
            if ev.seq is not None:
                row["_seq"] = ev.seq
            self.results[p["result_id"]] = row
            self.results[p["attempt_id"]] = row
            # Populate dep_outputs from named outputs for InputSpec.bind.
            dep_outputs = self.dep_outputs
            dep_provenance = self.dep_provenance
            aid = p.get("attempt_id")
            att = self.attempts.get(aid or "", {})
            tid = att.get("task_id") or p.get("task_id")
            if tid and tid in self.tasks:
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
                if prov in PROVENANCE_ORDER:
                    dep_provenance[tkey] = prov
        elif ev.type == "TaskDone":
            t = self.tasks[p["task_id"]]
            reason = coerce_task_reason(p.get("reason"))
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

    def _invalidate_verdict_snapshots_for_target(self, target: object) -> None:
        """Drop journaled snapshots for one target ref (DESIGN_M1 PR8)."""
        if target is None:
            return
        key = str(target)
        self.verdict_snapshots.pop(key, None)
        # Kind-prefixed keys (question:Q1 / hypothesis:H1) if present.
        if ":" not in key or key.split(":", 1)[0] not in ("question", "hypothesis"):
            self.verdict_snapshots.pop(f"question:{key}", None)
            self.verdict_snapshots.pop(f"hypothesis:{key}", None)

    def _fold_finalized_graph(
        self, study_id: str, payload: dict[str, object], *, created_at: datetime
    ) -> None:
        from coscientist.core.finalize.finalize_nodes import graph_rows_from_finalized

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
            rows = {str(k): dict(v) for k, v in hv.items() if isinstance(v, dict)}
            self.hypothesis_verdicts.update(rows)
            self._stamp_hypothesis_verdict_attrs(study_id, rows)

    # HYP v2 (docs/plan/hypothesis-v2.md §3.4): the schema verdict enum — the
    # fold only stamps verdicts produced by the real claims engine, never its own.
    _HYPOTHESIS_VERDICT_VALUES = frozenset(
        {"supported", "survived", "refuted", "inconclusive", "untested"}
    )

    def _stamp_hypothesis_verdict_attrs(
        self, study_id: str, rows: Mapping[str, Mapping[str, object]]
    ) -> None:
        """Durable verdicts → ``attrs.verdict`` on registered hypothesis nodes.

        Only existing nodes are stamped (old journals without registration stay
        as they are); values outside the schema verdict enum are skipped (the
        dict itself is already the report payload — the node attr is a
        projection for the graph digest / report, never a second source).
        """
        for hid, row in rows.items():
            v = row.get("verdict")
            if v not in self._HYPOTHESIS_VERDICT_VALUES:
                continue
            node = self.graph.get_node(f"{study_id}:hypothesis:{hid}")
            if node is None:
                continue
            attrs = dict(node.attrs)
            if attrs.get("verdict") == v:
                continue
            attrs["verdict"] = v
            self.graph.upsert_node(node.model_copy(update={"attrs": attrs}))
