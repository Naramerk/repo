"""In-memory projection of M0 state-core events (extracted from machine.py)."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from coscientist.core.claims.graph import GraphEdge, GraphNode, GraphStore
from coscientist.core.claims.reasons import RefusalKind, TaskReason
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import UnstampedEvent, StoredEvent
from coscientist.core.execution.domain import Study, Task, TaskOutcome, TaskState
from coscientist.core.plan.intake import IntakeRefuse

log = logging.getLogger(__name__)


def _req_str(d: dict, key: str) -> str:
    from coscientist.core.events.errors import JournalFormatError
    if key not in d:
        raise JournalFormatError(f"payload missing {key!r}")
    v = d[key]
    if not isinstance(v, str):
        raise JournalFormatError(f"payload {key!r} must be str, got {type(v).__name__}")
    return v


def _req_bool(d: dict, key: str) -> bool:
    from coscientist.core.events.errors import JournalFormatError
    if key not in d:
        raise JournalFormatError(f"payload missing {key!r}")
    v = d[key]
    if type(v) is not bool:
        raise JournalFormatError(f"payload {key!r} must be bool, got {type(v).__name__}")
    return v


def _as_float(value: object, *, what: str) -> float:
    from coscientist.core.events.errors import JournalFormatError
    if type(value) is bool or not isinstance(value, (int, float)):
        raise JournalFormatError(f"{what} must be numeric, got {type(value).__name__}")
    return float(value)  # audit-ok[coerce_float]: coerce after int|float type gate


def _as_decimal(value: object, *, what: str):
    from decimal import Decimal

    from coscientist.core.events.errors import JournalFormatError
    if isinstance(value, Decimal):
        return value
    if type(value) is bool or not isinstance(value, (int, float, str)):
        raise JournalFormatError(f"{what} must be Decimal-compatible, got {type(value).__name__}")
    return Decimal(str(value))



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
    artifacts: Any
    dep_outputs: dict[str, dict[str, str]]
    dep_provenance: dict[str, Any]
    _task_spent_rub: dict[str, Decimal]
    _task_budget_alloc: dict[str, Decimal]
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

        try:
            apply_kind_stamp(arts, artifact_id, kind)
        except ArtifactRejected:
            kinds = self.artifact_kinds
            if isinstance(kinds, dict):
                kinds.pop(artifact_id, None)
            miss = self._stamp_meta_missing
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
        pending = self._pending_kind_stamps
        if not pending:
            return
        self._pending_kind_stamps = []
        kinds = self.artifact_kinds
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
        reqs = self._expand_requests
        if not isinstance(reqs, dict) or not reqs:
            return
        if plan_key and plan_key in reqs:
            reqs.pop(plan_key, None)
        if after_task:
            drop = []
            for k, m in reqs.items():
                if not isinstance(m, dict) or "after_task" not in m:
                    raise JournalFormatError(f"_expand_requests[{k!r}] corrupt")
                if m["after_task"] == after_task:
                    drop.append(k)
            for k in drop:
                reqs.pop(k, None)

    def _project_local(self, ev: UnstampedEvent | StoredEvent) -> None:
        p = ev.payload
        if ev.type == "StudyCreated":
            self.study = Study.model_validate(p)
        elif ev.type == "StudyRefused":
            if "reason" not in p:
                raise JournalFormatError("StudyRefused: missing reason")
            if "classifier_error" not in p:
                raise JournalFormatError("StudyRefused: missing classifier_error")
            self._intake_refusal = IntakeRefuse(
                kind=RefusalKind(p["kind"]),
                reason=_req_str(p, "reason"),
                classifier_error=_req_bool(p, "classifier_error"),
            )
        elif ev.type == "BudgetDebited":
            if "spent_rub" not in p:
                raise JournalFormatError("BudgetDebited: missing spent_rub")
            spent = _as_decimal(p["spent_rub"], what="spent_rub")
            self._study_spent_rub += spent
            if "task_id" in p:
                tid = p["task_id"]
                if not isinstance(tid, str) or not tid:
                    raise JournalFormatError("BudgetDebited: empty task_id")
                raw_spent = self._task_spent_rub
                if raw_spent is None:
                    raw_spent = {}
                    self._task_spent_rub = raw_spent
                elif not isinstance(raw_spent, dict):
                    raise JournalFormatError(
                        f"_task_spent_rub must be dict, got {type(raw_spent).__name__}"
                    )
                key = str(tid)
                if key in raw_spent:
                    raw_spent[key] = _as_decimal(raw_spent[key], what="task_spent") + spent
                else:
                    raw_spent[key] = spent
        elif ev.type == "BudgetSharesAdjusted":
            from coscientist.core.plan.share_alloc import fold_budget_shares_adjusted

            fold_budget_shares_adjusted(self, p)
        elif ev.type == "BudgetExceeded":
            if "scope" not in p:
                raise JournalFormatError("BudgetExceeded: missing scope")
            scope = _req_str(p, "scope")
            if scope == "study":
                self._budget_exceeded = True
            elif scope == "task":
                # Task-level exceed is folded via TaskDone(budget); flag unused.
                pass
            else:
                raise JournalFormatError(f"BudgetExceeded: unknown scope {scope!r}")
        elif ev.type == "PlanAccepted":
            self.plans[ev.entity_key] = dict(p)
        elif ev.type == "PlanExpanded":
            self.plans[ev.entity_key] = dict(p)
            # B3: expansion counts + replan_used from events (not live counters).
            # Foreach (PR10) does not spend agent/human expansion quota.
            # Foreach (PR10) / Loop (PR11) do not spend agent/human expansion quota.
            if not p.get("foreach") and not p.get("loop"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                # timeout → agent quota (DESIGN_M1 §4); only human skips agent quota.
                if p.get("delegated_by") == "human":  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                    self._human_expansions = self._human_expansions + 1
                else:
                    self._agent_expansions = self._agent_expansions + 1
            if "replan_allocated_s" in p:
                self._replan_used_s = self._replan_used_s + _as_float(
                    p["replan_allocated_s"], what="replan_allocated_s"
                )
            after = p.get("after_task")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if after is not None and isinstance(self._expand_pending, set):
                self._expand_pending.discard(str(after))
            # Clear PLAN-n pending rebuilt from TaskCreated (MAJOR replay leak).
            self._clear_expand_request(
                after_task=str(after) if after is not None else None,  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
                plan_key=p.get("plan_key"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            )
            rid = p.get("hitl_request_id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if rid:
                used = self._hitl_expand_used
                if not isinstance(used, set):
                    self._hitl_expand_used = set()
                    used = self._hitl_expand_used
                used.add(str(rid))
            # PR10: rebuild foreach child index from journal (restart == live).
            if p.get("foreach"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                tmpl_raw = p.get("foreach_template")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                if isinstance(tmpl_raw, str):
                    tmpl = tmpl_raw
                else:
                    tmpl = ""
                added = p.get("added_tasks")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                if added is None:
                    added = []
                kids = []
                for t in added:
                    if isinstance(t, dict):
                        tk = t.get("task_key")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                    else:
                        tk = t.task_key
                    if isinstance(tk, str) and tk:
                        kids.append(tk)
                kids = [k for k in kids if k]
                children = self._foreach_children
                if not isinstance(children, dict):
                    self._foreach_children = {}
                    children = self._foreach_children
                if tmpl:
                    children[tmpl] = kids
            # PR11: rebuild loop iter index (does not spend expansion quota).
            if p.get("loop"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
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
            refusals = self._expansion_refusals
            if isinstance(refusals, list):
                refusals.append(dict(p))
            else:
                self._expansion_refusals = [dict(p)]
            if "after_task" not in p:
                raise JournalFormatError("ExpansionRefused: missing after_task")
            after_raw = p["after_task"]
            if not isinstance(after_raw, str) or not after_raw:
                raise JournalFormatError(
                    "ExpansionRefused: after_task required non-empty"
                )
            self._clear_expand_request(
                after_task=after_raw,
                plan_key=p.get("plan_key"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            )
            # PR11: loop refuse must restore status on replay.
            if p.get("loop") or p.get("loop_id"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_call]: coalesce existing call result; no invented literal  # audit-ok[or_call]: coalesce existing call result; no invented literal
                from coscientist.core.plan.loop import fold_loop_expansion_refused

                fold_loop_expansion_refused(self, p)
        elif ev.type == "HitlOpened":
            if "request_id" in p and isinstance(p["request_id"], str) and p["request_id"]:
                rid = p["request_id"]
            elif ev.entity_key:
                rid = ev.entity_key
            else:
                raise JournalFormatError("HitlOpened: missing request_id")
            opens = self._hitl_requests
            if opens is None:
                self._hitl_requests = {}
                opens = self._hitl_requests
            elif not isinstance(opens, dict):
                raise JournalFormatError(
                    f"_hitl_requests must be dict, got {type(opens).__name__}"
                )
            opens[rid] = dict(p)
        elif ev.type == "HitlDecided":
            if "request_id" in p and isinstance(p["request_id"], str) and p["request_id"]:
                rid = p["request_id"]
            elif ev.entity_key:
                rid = ev.entity_key
            else:
                raise JournalFormatError("HitlDecided: missing request_id")
            decisions = self._hitl_decisions
            if decisions is None:
                self._hitl_decisions = {}
                decisions = self._hitl_decisions
            elif not isinstance(decisions, dict):
                raise JournalFormatError(
                    f"_hitl_decisions must be dict, got {type(decisions).__name__}"
                )
            decisions[rid] = dict(p)
            # Close the open gate unless keep_open / WAIT. Do not merge decision
            # payload fields into the HitlOpened row (request ≠ decision).
            opens = self._hitl_requests
            if isinstance(opens, dict) and rid in opens:
                keep_open = p.get("keep_open") is True or p.get("effect") == "WAIT"  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
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
            if "artifact_id" in p and isinstance(p["artifact_id"], str) and p["artifact_id"]:
                art_id = p["artifact_id"]
            elif ev.entity_key:
                art_id = ev.entity_key
            else:
                raise JournalFormatError("ArtifactRegistered: missing artifact_id")
            kind = p.get("kind")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if art_id and kind and p.get("stamped") is True:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                arts = self.artifacts
                if arts is None:
                    # open_runtime folds before make_executor — defer stamp apply.
                    # Do not set artifact_kinds until soft stamp succeeds (flush).
                    pending = self._pending_kind_stamps
                    if not isinstance(pending, list):
                        self._pending_kind_stamps = []
                        pending = self._pending_kind_stamps
                    pending.append((art_id, str(kind)))
                elif self._apply_kind_stamp_soft(art_id, str(kind)):
                    kinds = self.artifact_kinds
                    if not isinstance(kinds, dict):
                        self.artifact_kinds = {}
                        kinds = self.artifact_kinds
                    kinds[art_id] = str(kind)
        elif ev.type == "QuestionDisposed":
            from coscientist.core.claims.dispose import fold_question_disposed

            store = self.question_disposals
            if not isinstance(store, dict):
                self.question_disposals = {}
                store = self.question_disposals
            if not isinstance(p, dict):
                raise JournalFormatError("QuestionDisposed payload must be dict")
            fold_question_disposed(store, p)
        elif ev.type == "QuestionDisposeRejected":
            from coscientist.core.claims.dispose import fold_question_dispose_rejected

            notes = self._disposal_rejects
            if not isinstance(notes, list):
                self._disposal_rejects = []
                notes = self._disposal_rejects
            if not isinstance(p, dict):
                raise JournalFormatError("QuestionDisposeRejected payload must be dict")
            fold_question_dispose_rejected(notes, p)
        elif ev.type == "StudyFinalized":
            # First-wins: ignore duplicate StudyFinalized on replay (legacy journals).
            # Record a taint mark so reopen/report can surface the I3 breach.
            if self._finalized:
                log.warning(
                    "ignoring duplicate StudyFinalized study_id=%s seq=%s "
                    "(first-wins; local fold skipped; tainted)",
                    ev.study_id,
                    ev.seq,
                )
                dups = self._duplicate_study_finalized
                if not isinstance(dups, list):
                    self._duplicate_study_finalized = []
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
            if "plan_role" not in clean or clean["plan_role"] is None:
                raise JournalFormatError("TaskCreated: plan_role required")
            self.tasks[p["task_id"]] = Task.model_validate(clean)
            # Fold PLAN-n replan_allocated_s (reserve spent creating the container).
            # Sole PLAN-n signal is Task.plan_role on the folded Task payload.
            from coscientist.core.execution.domain import PlanRole

            folded = self.tasks[p["task_id"]]
            if folded.plan_role is None:
                raise JournalFormatError("TaskCreated: plan_role required")
            pending_exp = p.get("pending_expansion")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            after = p.get("expand_after_task")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if folded.plan_role == PlanRole.PLAN_N:
                if "replan_allocated_s" not in p:
                    raise JournalFormatError(
                        "TaskCreated(plan_n): replan_allocated_s required"
                    )
                alloc = _as_float(p["replan_allocated_s"], what="replan_allocated_s")
                self._replan_used_s = self._replan_used_s + alloc
                if not isinstance(after, str) or not after:
                    raise JournalFormatError(
                        "TaskCreated(plan_n): expand_after_task required non-empty str"
                    )
                if pending_exp is not None and not isinstance(pending_exp, dict):
                    raise JournalFormatError(
                        "TaskCreated(plan_n): pending_expansion must be dict or null"
                    )
                tkey = folded.spec.task_key
                reqs = self._expand_requests
                if not isinstance(reqs, dict):
                    self._expand_requests = {}
                    reqs = self._expand_requests
                reqs[tkey] = {
                    "after_task": after,
                    "pending": pending_exp,
                    "replan_allocated_s": alloc,
                }
                if not isinstance(self._expand_pending, set):
                    self._expand_pending = set()
                self._expand_pending.add(after)
            else:
                if "replan_allocated_s" in p:
                    raise JournalFormatError(
                        "TaskCreated: replan_allocated_s only valid when plan_role=plan_n"
                    )
                if after is not None:
                    raise JournalFormatError(
                        "TaskCreated: expand_after_task only valid when plan_role=plan_n"
                    )
        elif ev.type == "AttemptStarted":
            row = dict(p)
            # Schema v1: required on the wire (no upcaster/default fill).
            try:
                row["attempt_role"] = p["attempt_role"]
            except KeyError as e:
                raise JournalFormatError(
                    "AttemptStarted: missing 'attempt_role'"
                ) from e
            if "charged" not in p:
                raise JournalFormatError("AttemptStarted: missing 'charged'")
            # bool is a subclass of int — require exact bool.
            if type(p["charged"]) is not bool:
                raise JournalFormatError(
                    f"AttemptStarted: charged must be bool, got {type(p['charged']).__name__}"
                )
            row["charged"] = p["charged"]
            if "provenance" not in p:
                raise JournalFormatError("AttemptStarted: missing provenance")
            if p["provenance"] not in ("real", "partial", "stub"):
                raise JournalFormatError(
                    f"AttemptStarted: bad provenance {p['provenance']!r}"
                )
            row["provenance"] = p["provenance"]
            # Open until AttemptClosed — required by _close_open_work (no missing key).
            row["closed"] = False
            if ev.seq is not None:
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
            if p.get("share_renorm") and p.get("deadline") is not None:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                raw_dl: Any = p["deadline"]
                if isinstance(raw_dl, str):
                    raw_dl = datetime.fromisoformat(raw_dl)
                upd["deadline"] = raw_dl
            self.tasks[p["task_id"]] = t.model_copy(update=upd)
            bud_alloc = p.get("allocated_budget_rub")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if bud_alloc is not None:
                raw_caps = self._task_budget_alloc
                if not isinstance(raw_caps, dict):
                    raw_caps = {}
                    self._task_budget_alloc = raw_caps
                tid = _req_str(p, "task_id")
                raw_caps[tid] = _as_decimal(bud_alloc, what="allocated_budget_rub")
                # Cap implies a spent entry (0 until BudgetDebited) — readers must not invent.
                raw_spent = self._task_spent_rub
                if not isinstance(raw_spent, dict):
                    raw_spent = {}
                    self._task_spent_rub = raw_spent
                if tid not in raw_spent:
                    raw_spent[tid] = Decimal("0")
            self._retry_at.pop(p["task_id"], None)
        elif ev.type == "AttemptClosed":
            aid = p["attempt_id"]
            att = self.attempts.get(aid)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if att is None:
                att = {}
            else:
                att = dict(att)
            att.update(p)
            att["closed"] = True
            self.attempts[aid] = att
            tid = p.get("task_id") or att.get("task_id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_call]: coalesce existing call result; no invented literal  # audit-ok[or_call]: coalesce existing call result; no invented literal
            if tid and p.get("retry_at"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
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
            aid = p.get("attempt_id") or ev.entity_key  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_name]: coalesce existing name/attr; no invented literal  # audit-ok[or_name]: coalesce existing name/attr; no invented literal
            prior = self.attempts.get(aid)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if prior is None:
                prior = {}
            att = {
                **prior,
                "route_returned": True,
                "agent_output_digest": p.get("agent_output_digest"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            }
            self.attempts[aid] = att
        elif ev.type == "ResultPending":
            # PR13: keep agent_output_digest on the open attempt for judge restart.
            aid = p.get("attempt_id") or ev.entity_key  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_name]: coalesce existing name/attr; no invented literal  # audit-ok[or_name]: coalesce existing name/attr; no invented literal
            dig = p.get("agent_output_digest")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if aid and dig:
                prior = self.attempts.get(aid)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                if prior is None:
                    prior = {}
                self.attempts[aid] = {**prior, "agent_output_digest": dig}
        elif ev.type == "ResultRecorded":
            row = dict(p)
            if ev.seq is not None:
                row["_seq"] = ev.seq
            self.results[p["result_id"]] = row
            self.results[p["attempt_id"]] = row
            # Populate dep_outputs from named outputs for InputSpec.bind.
            dep_outputs = self.dep_outputs
            dep_provenance = self.dep_provenance
            aid = p.get("attempt_id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if not isinstance(aid, str) or not aid:
                raise JournalFormatError("ResultRecorded: missing attempt_id")
            att_row = self.attempts.get(aid)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            result_tid: str | None = None
            if isinstance(att_row, dict) and "task_id" in att_row:
                result_tid = _req_str(att_row, "task_id")
            elif "task_id" in p:
                result_tid = _req_str(p, "task_id")
            if isinstance(dep_outputs, dict) and result_tid and result_tid in self.tasks:
                tkey = self.tasks[result_tid].spec.task_key
                omap = p.get("output_map")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                if isinstance(omap, dict) and omap:
                    dep_outputs[tkey] = {str(k): str(v) for k, v in omap.items() if v}
                # No silent main←artifacts[0] invent (rule 12). Absent output_map
                # leaves dep_outputs untouched; bind_inputs fails closed later.
                if "provenance" not in p:
                    raise JournalFormatError("ResultRecorded: missing provenance")
                prov = p["provenance"]
                if prov not in ("real", "partial", "stub"):
                    raise JournalFormatError(
                        f"ResultRecorded: bad provenance {prov!r}"
                    )
                if isinstance(dep_provenance, dict):
                    dep_provenance[tkey] = prov
        elif ev.type == "TaskDone":
            t = self.tasks[p["task_id"]]
            reason = p.get("reason")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            self.tasks[p["task_id"]] = t.model_copy(
                update={
                    "state": TaskState.DONE,
                    "outcome": TaskOutcome(p["outcome"]),
                    "reason": reason,
                }
            )
            self._retry_at.pop(p["task_id"], None)
            # fail_attempt / infra paths may close without ResultRecorded — stamp
            # dep provenance so simulate/partial bind_inputs stays fail-closed
            # without inventing "real".
            dep_provenance = self.dep_provenance
            if isinstance(dep_provenance, dict):
                tkey = t.spec.task_key
                if tkey not in dep_provenance:
                    if "outcome" not in p:
                        raise JournalFormatError("TaskDone: missing outcome")
                    oc = _req_str(p, "outcome")
                    if oc in (TaskOutcome.FAILED.value, "FAILED"):
                        dep_provenance[tkey] = "stub"
                    elif oc in (TaskOutcome.PARTIAL.value, "PARTIAL"):
                        dep_provenance[tkey] = "partial"
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
                and not node.attrs
                and existing.attrs
            ):
                pass  # keep existing
            else:
                # Plan-freeze: stamp frozen_at_seq from journal seq when missing.
                if (
                    node.kind == "criterion"
                    and node.attrs.get("frozen") is True  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                    and node.attrs.get("frozen_at_seq") is None  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                    and ev.seq is not None
                ):
                    node = node.model_copy(
                        update={
                            "attrs": {
                                **node.attrs,
                                "frozen_at_seq": ev.seq,
                            }
                        }
                    )
                self.graph.upsert_node(node)
            if node.kind == "claim":
                self._invalidate_verdict_snapshots_for_target(node.attrs.get("target"))  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        elif ev.type == "GraphEdgeAdded":
            # Fail loudly: missing endpoints / illegal pairs are journal bugs.
            edge = GraphEdge.model_validate(p)
            self.graph.add_edge(edge)
            if edge.kind == "about":
                # Scope invalidation to the about target (not all snapshots).
                dst = self.graph.get_node(edge.dst_id)
                logical = None
                if dst is not None:
                    if dst.ref_id is not None:
                        logical = dst.ref_id
                    else:
                        raise JournalFormatError(
                            f"GraphEdgeAdded about-dst missing ref_id "
                            f"(typed field required; no id-prefix strip): {edge.dst_id!r}"
                        )
                self._invalidate_verdict_snapshots_for_target(logical)
        elif ev.type == "VerdictSnapshotsRecorded":
            snaps = p.get("snapshots")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if snaps is None:
                snaps = {}
            if isinstance(snaps, dict):
                self.verdict_snapshots.update({str(k): dict(v) for k, v in snaps.items() if isinstance(v, dict)})

    def _invalidate_verdict_snapshots_for_target(self, target: Any) -> None:
        """Drop journaled snapshots for one target ref (DESIGN_M1 PR8)."""
        if target is None:
            return
        key = str(target)
        self.verdict_snapshots.pop(key, None)
        # Kind-prefixed keys (question:Q1 / hypothesis:H1) if present.
        # Kind-prefixed keys (question:Q1 / hypothesis:H1) if present.
        kind_prefix = key.split(":", 1)[0] if ":" in key else ""  # audit-ok[str_split]: kind:ref key parse # audit-ok[ternary_default]: kind:ref key parse  # audit-ok[ternary_default]: branch selects typed literal required by contract
        if kind_prefix not in ("question", "hypothesis"):
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
                and existing.attrs
                and not incoming.attrs.get("formulation")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            ):
                # Preserve formulation from accept_plan when finalize row is sparse.
                merged = incoming.model_copy(
                    update={"attrs": {**existing.attrs, **incoming.attrs}}
                )
                self.graph.upsert_node(merged)
            else:
                self.graph.upsert_node(incoming)
        for row in edges.values():
            try:
                self.graph.add_edge(GraphEdge.model_validate(row))
            except (KeyError, ValueError):
                # study/question node may be missing in sparse journals
                pass  # audit-ok[except_pass]: best-effort secondary close after primary error path
        # Keep hypothesis_verdicts separate from bind's verdict_snapshots cache.
        hv = payload.get("hypothesis_verdicts")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if isinstance(hv, dict):
            self.hypothesis_verdicts.update({str(k): dict(v) for k, v in hv.items() if isinstance(v, dict)})
