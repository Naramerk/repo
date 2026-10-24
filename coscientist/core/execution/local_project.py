"""In-memory projection of M0 state-core events (extracted from machine.py)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from coscientist.core.claims.graph import GraphEdge, GraphNode, GraphStore
from coscientist.core.claims.reasons import RefusalKind, TaskReason
from coscientist.core.events.errors import JournalFormatError, require_journal_field
from coscientist.core.events.events import EventEnvelope
from coscientist.core.execution.domain import Study, Task, TaskKind, TaskOutcome, TaskState
from coscientist.core.plan.intake import IntakeRefuse

log = logging.getLogger(__name__)


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
        arts = getattr(self, "artifacts", None)
        if arts is None:
            return False
        from coscientist.core.events.errors import ArtifactRejected
        from coscientist.core.execution.artifact_stamp import apply_kind_stamp

        try:
            apply_kind_stamp(arts, artifact_id, kind)
        except ArtifactRejected:
            kinds = getattr(self, "artifact_kinds", None)
            if isinstance(kinds, dict):
                kinds.pop(artifact_id, None)
            miss = getattr(self, "_stamp_meta_missing", None)
            if not isinstance(miss, list):
                self._stamp_meta_missing = []
                miss = self._stamp_meta_missing
            miss.append(
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
        pending = getattr(self, "_pending_kind_stamps", None)
        if not pending:
            return
        self._pending_kind_stamps = []
        kinds = getattr(self, "artifact_kinds", None)
        if not isinstance(kinds, dict):
            self.artifact_kinds = {}
            kinds = self.artifact_kinds
        for art_id, kind in pending:
            if self._apply_kind_stamp_soft(art_id, kind):
                kinds[art_id] = kind

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
            for k, m in list(reqs.items()):
                raw_after = require_journal_field(m, "after_task", ctx="expand_request")
                if str(raw_after) == after_task:
                    reqs.pop(k, None)

    def _project_local(self, ev: EventEnvelope) -> None:
        p = ev.payload
        if ev.type == "StudyCreated":
            self.study = Study.model_validate(p)
        elif ev.type == "StudyRefused":
            self._intake_refusal = IntakeRefuse(
                kind=RefusalKind(p["kind"]),
                reason=str(require_journal_field(p, "reason", ctx="StudyRefused")),
                classifier_error=bool(
                    require_journal_field(p, "classifier_error", ctx="StudyRefused")
                ),
            )
        elif ev.type == "BudgetDebited":
            spent = Decimal(
                str(require_journal_field(p, "spent_rub", ctx="BudgetDebited"))
            )
            self._study_spent_rub += spent
            tid = p.get("task_id")
            if tid:
                raw_spent = getattr(self, "_task_spent_rub", None)
                if not isinstance(raw_spent, dict):
                    raw_spent = {}
                    self._task_spent_rub: dict[str, Decimal] = raw_spent
                prev = raw_spent.get(str(tid))
                base = Decimal(0) if prev is None else Decimal(str(prev))
                raw_spent[str(tid)] = base + spent
        elif ev.type == "BudgetSharesAdjusted":
            from coscientist.core.plan.share_alloc import fold_budget_shares_adjusted

            fold_budget_shares_adjusted(self, p)
        elif ev.type == "BudgetExceeded":
            scope = str(require_journal_field(p, "scope", ctx="BudgetExceeded"))
            if scope == "study":
                self._budget_exceeded = True
            elif scope == "task":
                # Task-level exceed is folded via TaskDone(budget); flag unused.
                pass
            else:
                raise JournalFormatError(
                    f"unknown BudgetExceeded scope: {scope!r}",
                    field="scope",
                )
        elif ev.type == "PlanAccepted":
            self.plans[ev.entity_key] = dict(p)
        elif ev.type == "PlanExpanded":
            self.plans[ev.entity_key] = dict(p)
            # B3: expansion counts + replan_used from events (not live counters).
            # Foreach (PR10) does not spend agent/human expansion quota.
            # Foreach (PR10) / Loop (PR11) do not spend agent/human expansion quota.
            if not p.get("foreach") and not p.get("loop"):
                # timeout → agent quota (DESIGN_M1 §4); only human skips agent quota.
                # Counters are initialized on StudyRuntime.__init__ (never getattr default).
                if p.get("delegated_by") == "human":
                    self._human_expansions = int(self._human_expansions) + 1
                else:
                    self._agent_expansions = int(self._agent_expansions) + 1
            if "replan_allocated_s" in p:
                alloc = float(
                    str(require_journal_field(p, "replan_allocated_s", ctx="PlanExpanded"))
                )
                if alloc:
                    self._replan_used_s = float(self._replan_used_s) + alloc
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
                tmpl = str(
                    require_journal_field(p, "foreach_template", ctx="PlanExpanded")
                )
                added = require_journal_field(p, "added_tasks", ctx="PlanExpanded")
                if not isinstance(added, list):
                    raise JournalFormatError(
                        "foreach PlanExpanded added_tasks must be a list",
                        field="added_tasks",
                    )
                kids: list[str] = []
                for t in added:
                    if isinstance(t, dict):
                        tk = t.get("task_key")
                    else:
                        tk = getattr(t, "task_key", None)
                    if tk is None or str(tk) == "":
                        raise JournalFormatError(
                            "foreach added_tasks entry missing task_key",
                            field="task_key",
                        )
                    kids.append(str(tk))
                children = getattr(self, "_foreach_children", None)
                if not isinstance(children, dict):
                    self._foreach_children: dict[str, list[str]] = {}
                    children = self._foreach_children
                children[tmpl] = kids
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
            refusals = getattr(self, "_expansion_refusals", None)
            if isinstance(refusals, list):
                refusals.append(dict(p))
            else:
                self._expansion_refusals = [dict(p)]
            after_raw = p.get("after_task")
            after_s = (
                str(after_raw)
                if after_raw is not None and str(after_raw) != ""
                else None
            )
            self._clear_expand_request(
                after_task=after_s,
                plan_key=p.get("plan_key"),
            )
            # PR11: loop refuse must restore status on replay.
            if p.get("loop") or p.get("loop_id"):
                from coscientist.core.plan.loop import fold_loop_expansion_refused

                fold_loop_expansion_refused(self, p)
        elif ev.type == "HitlOpened":
            rid_raw = require_journal_field(p, "request_id", ctx="HitlOpened")
            if rid_raw is None or str(rid_raw) == "":
                raise JournalFormatError(
                    "HitlOpened missing request_id",
                    field="request_id",
                )
            rid = str(rid_raw)
            opens = getattr(self, "_hitl_requests", None)
            if not isinstance(opens, dict):
                self._hitl_requests = {}
                opens = self._hitl_requests
            opens[rid] = dict(p)
        elif ev.type == "HitlDecided":
            rid_raw = require_journal_field(p, "request_id", ctx="HitlDecided")
            if rid_raw is None or str(rid_raw) == "":
                raise JournalFormatError(
                    "HitlDecided missing request_id",
                    field="request_id",
                )
            rid = str(rid_raw)
            decisions = getattr(self, "_hitl_decisions", None)
            if not isinstance(decisions, dict):
                self._hitl_decisions = {}
                decisions = self._hitl_decisions
            decisions[rid] = dict(p)
            # Close the open gate unless keep_open / WAIT. Do not merge decision
            # payload fields into the HitlOpened row (request ≠ decision).
            opens = getattr(self, "_hitl_requests", None)
            if isinstance(opens, dict) and rid in opens:
                keep_open = bool(p.get("keep_open") or p.get("effect") == "WAIT")
                opens[rid] = {**opens[rid], "open": keep_open}
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
            art_raw = require_journal_field(p, "artifact_id", ctx="ArtifactRegistered")
            if art_raw is None or str(art_raw) == "":
                raise JournalFormatError(
                    "ArtifactRegistered missing artifact_id",
                    field="artifact_id",
                )
            art_id = str(art_raw)
            kind = p.get("kind")
            if kind and bool(p.get("stamped")):
                arts = getattr(self, "artifacts", None)
                if arts is None:
                    # open_runtime folds before make_executor — defer stamp apply.
                    # Do not set artifact_kinds until soft stamp succeeds (flush).
                    pending = getattr(self, "_pending_kind_stamps", None)
                    if not isinstance(pending, list):
                        self._pending_kind_stamps = []
                        pending = self._pending_kind_stamps
                    pending.append((art_id, str(kind)))
                elif self._apply_kind_stamp_soft(art_id, str(kind)):
                    kinds = getattr(self, "artifact_kinds", None)
                    if not isinstance(kinds, dict):
                        self.artifact_kinds = {}
                        kinds = self.artifact_kinds
                    kinds[art_id] = str(kind)
        elif ev.type == "QuestionDisposed":
            from coscientist.core.claims.dispose import fold_question_disposed

            store = getattr(self, "question_disposals", None)
            if not isinstance(store, dict):
                self.question_disposals = {}
                store = self.question_disposals
            fold_question_disposed(store, p if isinstance(p, dict) else {})
        elif ev.type == "QuestionDisposeRejected":
            from coscientist.core.claims.dispose import fold_question_dispose_rejected

            notes = getattr(self, "_disposal_rejects", None)
            if not isinstance(notes, list):
                self._disposal_rejects = []
                notes = self._disposal_rejects
            fold_question_dispose_rejected(notes, p if isinstance(p, dict) else {})
        elif ev.type == "StudyFinalized":
            # First-wins: ignore duplicate StudyFinalized on replay (legacy journals).
            # Record a taint mark so reopen/report can surface the I3 breach.
            if self._finalized:
                log.warning(
                    "ignoring duplicate StudyFinalized study_id=%s seq=%s "
                    "(first-wins; local fold skipped; tainted)",
                    ev.study_id,
                    getattr(ev, "seq", None),
                )
                dups = getattr(self, "_duplicate_study_finalized", None)
                if not isinstance(dups, list):
                    self._duplicate_study_finalized = []
                    dups = self._duplicate_study_finalized
                dups.append(
                    {
                        "study_id": ev.study_id,
                        "seq": getattr(ev, "seq", None),
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
            alloc = 0.0
            if "replan_allocated_s" in p:
                alloc = float(
                    str(require_journal_field(p, "replan_allocated_s", ctx="TaskCreated"))
                )
                if alloc:
                    self._replan_used_s = float(self._replan_used_s) + alloc
            pending_exp = p.get("pending_expansion")
            after = p.get("expand_after_task")
            spec = clean.get("spec")
            if after and clean.get("kind") == TaskKind.PLAN.value:
                if not isinstance(spec, Mapping):
                    raise JournalFormatError(
                        "PLAN TaskCreated missing spec",
                        field="spec",
                    )
                tkey = str(
                    require_journal_field(spec, "task_key", ctx="TaskCreated")
                )
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
            # Writer stamps closed/charged/provenance/attempt_role. Schema v10
            # refusal of legacy journals is deferred until after #38 rebase.
            row = dict(p)
            require_journal_field(row, "attempt_role", ctx="AttemptStarted")
            if getattr(ev, "seq", None) is not None:
                row["_seq"] = ev.seq
            self.attempts[p["attempt_id"]] = row
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
            aid_raw = require_journal_field(p, "attempt_id", ctx="RouteReturned")
            if aid_raw is None or str(aid_raw) == "":
                raise JournalFormatError(
                    "RouteReturned missing attempt_id",
                    field="attempt_id",
                )
            aid = str(aid_raw)
            if aid not in self.attempts:
                raise JournalFormatError(
                    f"RouteReturned for unknown attempt {aid!r}",
                    field="attempt_id",
                )
            att = self.attempts[aid]
            self.attempts[aid] = {
                **att,
                "route_returned": True,
                "agent_output_digest": p.get("agent_output_digest"),
            }
        elif ev.type == "ResultPending":
            # PR13: keep agent_output_digest on the open attempt for judge restart.
            aid_raw = require_journal_field(p, "attempt_id", ctx="ResultPending")
            if aid_raw is None or str(aid_raw) == "":
                raise JournalFormatError(
                    "ResultPending missing attempt_id",
                    field="attempt_id",
                )
            aid = str(aid_raw)
            dig = p.get("agent_output_digest")
            if dig:
                if aid not in self.attempts:
                    raise JournalFormatError(
                        f"ResultPending for unknown attempt {aid!r}",
                        field="attempt_id",
                    )
                att = self.attempts[aid]
                self.attempts[aid] = {**att, "agent_output_digest": dig}
        elif ev.type == "ResultRecorded":
            row = dict(p)
            if getattr(ev, "seq", None) is not None:
                row["_seq"] = ev.seq
            self.results[p["result_id"]] = row
            self.results[p["attempt_id"]] = row
            # Populate dep_outputs from named outputs for InputSpec.bind.
            dep_outputs = getattr(self, "dep_outputs", None)
            dep_provenance = getattr(self, "dep_provenance", None)
            aid_raw = p.get("attempt_id")
            if aid_raw is None or str(aid_raw) == "":
                raise JournalFormatError(
                    "ResultRecorded missing required attempt_id",
                    field="attempt_id",
                )
            aid = str(aid_raw)
            att_row = self.attempts.get(aid)
            if att_row is None:
                raise JournalFormatError(
                    f"ResultRecorded for unknown attempt {aid!r}",
                    field="attempt_id",
                )
            tid = att_row.get("task_id") or p.get("task_id")
            if isinstance(dep_outputs, dict) and tid and tid in self.tasks:
                tkey = self.tasks[tid].spec.task_key
                omap = p.get("output_map")
                if isinstance(omap, dict) and omap:
                    dep_outputs[tkey] = {str(k): str(v) for k, v in omap.items() if v}
                elif p.get("artifacts"):
                    raise JournalFormatError(
                        "ResultRecorded has artifacts without output_map "
                        f"(task_key={tkey!r})",
                        field="output_map",
                    )
                prov = require_journal_field(p, "provenance", ctx="ResultRecorded")
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
                    if dst.ref_id is None or str(dst.ref_id) == "":
                        raise JournalFormatError(
                            f"about-edge dst {edge.dst_id!r} missing ref_id",
                            field="ref_id",
                        )
                    logical = str(dst.ref_id)
                self._invalidate_verdict_snapshots_for_target(logical)
        elif ev.type == "VerdictSnapshotsRecorded":
            snaps = require_journal_field(p, "snapshots", ctx="VerdictSnapshotsRecorded")
            if not isinstance(snaps, dict):
                raise JournalFormatError(
                    "VerdictSnapshotsRecorded snapshots must be a mapping",
                    field="snapshots",
                )
            self.verdict_snapshots.update(
                {
                    str(k): dict(v)
                    for k, v in snaps.items()
                    if isinstance(v, dict)
                }
            )

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
                edge = GraphEdge.model_validate(row)
            except (TypeError, ValueError) as e:
                raise JournalFormatError(
                    f"invalid finalized graph edge payload: {e}",
                    field="edges",
                ) from e
            try:
                self.graph.add_edge(edge)
            except KeyError as e:
                # Sparse StudyFinalized payloads may omit study/question nodes;
                # never invent endpoints — skip the edge and keep a taint note.
                notes = getattr(self, "_sparse_finalized_edge_skips", None)
                if not isinstance(notes, list):
                    self._sparse_finalized_edge_skips: list[str] = []
                    notes = self._sparse_finalized_edge_skips
                notes.append(str(e))
        # Keep hypothesis_verdicts separate from bind's verdict_snapshots cache.
        hv = payload.get("hypothesis_verdicts")
        if isinstance(hv, dict):
            self.hypothesis_verdicts.update({str(k): dict(v) for k, v in hv.items() if isinstance(v, dict)})
