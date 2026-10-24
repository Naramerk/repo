"""Attempt start / fail / cancel / retry (extracted to keep machine.py ≤ 500)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from coscientist.core.claims.claims import compute_verdict_snapshots
from coscientist.core.claims.reasons import CloseReason, TaskReason, coerce_close_reason, coerce_task_reason
from coscientist.core.events.errors import JournalFormatError, require_journal_field
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.ids import attempt_id, task_id
from coscientist.core.execution.domain import (
    VALID_PROVENANCE,
    Provenance,
    Task,
    TaskKind,
    TaskOutcome,
    TaskState,
    worst_provenance,
)
from coscientist.core.execution.inputs import (
    InputUnavailable,
    bind_inputs,
    collect_bindable_claims,
    uses_claim_graph_events,
)
from coscientist.core.execution.quorum import bind_input_groups

# Persisted on AttemptClosed.retry_at; fired by StudyRuntime.tick vs injected Clock.
# Source: DESIGN P3 retry backoff — short intentional delay before tick restarts
# the next attempt; not a domain default guessed from data.
RETRY_BACKOFF_S = 0.05

# TaskReason → CloseReason for cancel_task AttemptClosed (explicit; no silent CANCELLED).
_CANCEL_CLOSE_REASON: dict[str, CloseReason] = {
    CloseReason.RESULT.value: CloseReason.RESULT,
    CloseReason.FAILED.value: CloseReason.FAILED,
    CloseReason.CRASHED.value: CloseReason.CRASHED,
    CloseReason.DEADLINE.value: CloseReason.DEADLINE,
    CloseReason.STUDY_DEADLINE.value: CloseReason.STUDY_DEADLINE,
    CloseReason.CANCELLED.value: CloseReason.CANCELLED,
    CloseReason.INPUT_UNAVAILABLE.value: CloseReason.INPUT_UNAVAILABLE,
    CloseReason.HUMAN_TIMEOUT.value: CloseReason.HUMAN_TIMEOUT,
    CloseReason.BUDGET.value: CloseReason.BUDGET,
    # TaskReason-only values used on cancel paths (AttemptClosed vocabulary).
    TaskReason.HUMAN.value: CloseReason.CANCELLED,
    TaskReason.HUMAN_REJECTED.value: CloseReason.CANCELLED,
    TaskReason.CRITIC_REJECTED.value: CloseReason.CANCELLED,
    TaskReason.REFUSED.value: CloseReason.CANCELLED,
    TaskReason.SUPERSEDED.value: CloseReason.CANCELLED,
    TaskReason.EXPANSION_LIMIT.value: CloseReason.CANCELLED,
    TaskReason.ITERATION_CAP.value: CloseReason.CANCELLED,
    TaskReason.PROPOSAL_QUOTA.value: CloseReason.CANCELLED,
    TaskReason.CATALOG_ERROR.value: CloseReason.FAILED,
    TaskReason.SANDBOX_INFRA.value: CloseReason.FAILED,
    TaskReason.SANDBOX_TIMEOUT.value: CloseReason.FAILED,
    TaskReason.IDLE_TIMEOUT.value: CloseReason.FAILED,
    TaskReason.CORE_BUG.value: CloseReason.CRASHED,
    TaskReason.SCHEMA_INVALID.value: CloseReason.FAILED,
    TaskReason.INVALID_STATUS.value: CloseReason.FAILED,
    TaskReason.SAME_DIGEST_RETRY.value: CloseReason.CANCELLED,
    TaskReason.RETRY_SCHEDULED.value: CloseReason.CANCELLED,
}


def _close_reason_for_cancel(reason_s: str) -> str:
    """Map cancel TaskReason → CloseReason.value or raise JournalFormatError."""
    try:
        return _CANCEL_CLOSE_REASON[reason_s].value
    except KeyError as e:
        raise JournalFormatError(
            f"unmapped cancel TaskReason for AttemptClosed: {reason_s!r}",
            field="close_reason",
        ) from e


def _attempt_closed(att: dict[str, Any], *, ctx: str) -> bool:
    """True if attempt is closed.

    Writers stamp ``closed`` on AttemptStarted. Legacy journals (pre-v10) omit
    it until AttemptClosed — absent means still open. Full refusal of missing
    closed awaits AttemptStarted v10 after #38.
    """
    del ctx  # reserved for v10 require_journal_field
    return bool(att.get("closed"))


def _attempt_charged(att: dict[str, Any], *, ctx: str) -> bool:
    """True if attempt is charged (absent on legacy journals ⇒ not charged)."""
    del ctx
    return bool(att.get("charged"))


def _attempt_role_from_reviews(runtime: Any, task_id: str) -> str:
    """Derive AttemptStarted.attempt_role from pending ReviewScheduled rows."""
    reviews = getattr(runtime, "_proposed_check_reviews", None) or {}
    tid = str(task_id)
    for rev in reviews.values():
        if not isinstance(rev, dict):
            continue
        status = require_journal_field(rev, "status", ctx="review")
        reviewer_task_id = require_journal_field(rev, "reviewer_task_id", ctx="review")
        if str(status) != "pending":
            continue
        if str(reviewer_task_id) == tid:
            return "reviewer"
    return "agent"

# Stage DAG (SPEC→…→REPORT): H-c must not cascade — DESIGN §7.5: failed SPEC
# → plan is built from the raw request; later stages still run.
_STAGE_KINDS = frozenset(
    {
        TaskKind.SPEC,
        TaskKind.HYPOTHESES,
        TaskKind.LITERATURE,
        TaskKind.PLAN,
        TaskKind.REPORT,
    }
)


class AttemptsMixin:
    """P2/P3/P6 attempt lifecycle methods for StudyRuntime."""

    study: Any
    tasks: dict[str, Task]
    attempts: dict[str, dict[str, Any]]
    clock: Any
    max_attempts: int
    dep_outputs: dict[str, dict[str, str]]
    dep_provenance: dict[str, Provenance]
    _attempt_tasks: dict[str, asyncio.Task[Any]]
    _lease_lost: bool
    _retry_at: dict[str, datetime]

    async def _append(self, events: list[EventEnvelope]) -> None:
        raise NotImplementedError

    def _lookup_dep(self, key: str) -> Task | None:
        dep_id = task_id(self.study.study_id, key) if self.study else key
        dep = self.tasks.get(dep_id) or self.tasks.get(key)
        if dep is None:
            matches = [t for t in self.tasks.values() if t.spec.task_key == key]
            dep = matches[0] if matches else None
        return dep

    def _deps_done(self, task: Task) -> bool:
        children_map = getattr(self, "_foreach_children", None)
        if children_map is None:
            raise JournalFormatError(
                "runtime._foreach_children required",
                field="_foreach_children",
            )
        for d in task.spec.depends_on:
            # PR11: loop:<id> barrier — ready after until-met or LoopCapped.
            # Unknown / dropped loop never becomes ready; ``refuse_orphaned_loop_barriers``
            # SKIPPED-refuses dependents (fail-closed, no hang).
            if str(d).startswith("loop:"):
                from coscientist.core.plan.loop import loop_is_complete, parse_loop_dep

                lid = parse_loop_dep(d)
                if lid is None or not loop_is_complete(self, lid):  # type: ignore[arg-type]
                    return False
                continue
            # PR10: depends_on a foreach template → wait for all concrete children.
            if d in children_map:
                for ck in children_map[d]:
                    child = self._lookup_dep(ck)
                    if child is None or child.state != TaskState.DONE:
                        return False
                continue
            dep = self._lookup_dep(d)
            if dep is None or dep.state != TaskState.DONE:
                return False
        return True

    def _dep_map(self) -> dict[str, Task]:
        return {t.spec.task_key: t for t in self.tasks.values()}

    def _hc_cascade_parent(self, dep: Task) -> bool:
        """True when H-c should skip children of this DONE parent.

        Scientific FAILED (``result_class=scientific``) does **not** cascade —
        it is a valid research outcome. SKIPPED and infra FAILED still cascade.
        ``fail_attempt`` without a ResultRecorded still cascades (legacy/infra).
        """
        if dep.outcome == TaskOutcome.SKIPPED:
            return True
        if dep.outcome != TaskOutcome.FAILED:
            return False
        results = getattr(self, "results", None) or {}
        for aid in reversed(dep.attempts):
            row = results.get(aid)
            if not isinstance(row, dict):
                continue
            rc = str(row.get("result_class") or "")
            if rc == "scientific":
                return False
            if rc == "infra":
                return True
            # Legacy FAILED row without class → treat as cascade.
            return True
        # No result row (e.g. fail_attempt) → cascade.
        return True

    async def _skip_unavailable_dep_children(self) -> None:
        """H-c: FAILED/SKIPPED parent without InputSpec → SKIPPED(input_unavailable).

        Stage tasks are excluded (failed SPEC must not wipe PLAN/REPORT). Skipped
        when ``tolerate_failed_dep`` is set. Parents covered by an ``InputSpec``
        are left to ``bind_inputs`` on start_attempt. Scientific FAILED parents
        do not cascade.
        """
        for task in list(self.tasks.values()):
            if task.state == TaskState.DONE:
                continue
            if task.kind in _STAGE_KINDS:
                continue
            if task.spec.tolerate_failed_dep:
                continue
            covered = {inp.from_task for inp in task.spec.inputs}
            for d in task.spec.depends_on:
                if d in covered:
                    continue
                dep = self._lookup_dep(d)
                if dep is None or dep.state != TaskState.DONE:
                    continue
                if self._hc_cascade_parent(dep):
                    await self.cancel_task(task.task_id, reason=TaskReason.INPUT_UNAVAILABLE)
                    break

    def _resolve_task(self, key_or_id: str) -> Task:
        if key_or_id in self.tasks:
            return self.tasks[key_or_id]
        matches = [t for t in self.tasks.values() if t.spec.task_key == key_or_id]
        if not matches:
            raise KeyError(key_or_id)
        return matches[0]

    def iter_dispatchable_tasks(self) -> list[Task]:
        """PENDING tasks a driver may start (excludes foreach templates).

        External drivers should iterate this list — never call ``start_attempt``
        on a template with ``spec.foreach`` set.
        """
        out: list[Task] = []
        for task in self.tasks.values():
            if task.state != TaskState.PENDING:
                continue
            if task.spec.foreach is not None:
                continue
            if not self._deps_done(task):
                continue
            open_atts = [
                a
                for a in task.attempts
                if not _attempt_closed(self.attempts[a], ctx=a)
            ]
            if open_atts:
                continue
            out.append(task)
        return out

    async def start_attempt(
        self,
        task_key_or_id: str,
        *,
        provenance: Provenance | None = None,
    ) -> str | None:
        """Start an attempt, or return ``None`` when the task is refused without one.

        Callers must treat ``None`` as a successful refusal (task already
        ``SKIPPED(budget)`` / similar) — never pass it to ``Executor.record``.

        Reviewer role for proposed_check is **not** caller-chosen: when this
        task is ``ReviewScheduled.reviewer_task_id`` for a pending review,
        AttemptStarted journals ``attempt_role=reviewer``; otherwise ``agent``.
        """
        task = self._resolve_task(task_key_or_id)
        # Foreach templates are materialization placeholders — never dispatched.
        if task.spec.foreach is not None:
            raise RuntimeError("foreach template cannot be started")
        if task.state == TaskState.DONE:
            raise RuntimeError("task already DONE")
        if not self._deps_done(task):
            raise RuntimeError("dependencies not DONE")
        open_atts = [
            a
            for a in task.attempts
            if not _attempt_closed(self.attempts[a], ctx=a)
        ]
        if open_atts:
            raise RuntimeError("already has open attempt")
        # Only charged attempts consume the retry budget (infra does not charge).
        # Sequence ``n`` is always len+1 so uncharged retries keep unique attempt ids.
        charged = 0
        for aid in task.attempts:
            att = self.attempts[aid]
            if not _attempt_closed(att, ctx=aid):
                raise RuntimeError(f"open attempt in budget count: {aid!r}")
            if _attempt_charged(att, ctx=aid):
                charged += 1
        if charged + 1 > self.max_attempts:
            raise RuntimeError("max_attempts exceeded")
        n = len(task.attempts) + 1

        graph = getattr(self, "graph", None)
        results = getattr(self, "results", None) or {}
        known_claims, claim_provenance = collect_bindable_claims(graph, results=results)
        # Prefer journaled snapshots (replay); else compute scoped to this task.
        stored = getattr(self, "verdict_snapshots", None) or {}
        verdict_refs = [inp.ref for inp in task.spec.inputs if inp.kind == "verdict" and inp.ref]
        verdict_snapshots: dict[str, dict] = {r: stored[r] for r in verdict_refs if r in stored}
        need = [r for r in verdict_refs if r not in verdict_snapshots]
        if need and graph is not None and self.study is not None:
            try:
                # Scope any_exp to covering tasks for these question refs when possible.
                cover_keys: set[str] = set()
                for plan in (getattr(self, "plans", None) or {}).values():
                    for spec in plan.get("tasks") or []:
                        addrs = set(spec.get("addresses") or [])
                        if addrs & set(need):
                            cover_keys.add(str(spec.get("task_key") or ""))
                cover_tasks = [t for t in self.tasks.values() if t.spec.task_key in cover_keys] or list(
                    self.tasks.values()
                )
                any_exp = any(
                    t.state == TaskState.DONE
                    and t.outcome
                    in (
                        TaskOutcome.SUCCESS,
                        TaskOutcome.PARTIAL,
                        TaskOutcome.FAILED,
                    )
                    for t in cover_tasks
                )
                computed = compute_verdict_snapshots(
                    graph,
                    study_id=self.study.study_id,
                    study=self.study,
                    results=results,
                    any_experiment_done=any_exp,
                    refs=need,
                )
                verdict_snapshots.update(computed)
            except Exception:  # noqa: BLE001 — start_attempt must not crash
                verdict_snapshots = {**verdict_snapshots}
        from coscientist.core.plan.foreach import expand_input_groups

        # PR10: foreach children expand; static quorum inputs share one pool.
        groups = expand_input_groups(self, list(task.spec.inputs))
        children_map = getattr(self, "_foreach_children", None)
        if children_map is None:
            raise JournalFormatError(
                "runtime._foreach_children required",
                field="_foreach_children",
            )
        fe_from = set(children_map)
        for t in self.tasks.values():
            if t.spec.foreach is not None:
                fe_from.add(t.spec.task_key)
        use_groups = any(
            inp.from_task in fe_from or inp.quorum is not None for inp, _ in groups
        )
        limitations: list[Any] = []
        try:
            if use_groups:
                bindings, derived, limitations = bind_input_groups(
                    groups,
                    foreach_from_tasks=fe_from,
                    dep_tasks=self._dep_map(),
                    dep_outputs=self.dep_outputs,
                    dep_provenance=self.dep_provenance,
                    known_claim_ids=known_claims,
                    claim_provenance=claim_provenance,
                    verdict_snapshots=verdict_snapshots,
                )
            else:
                bindings, derived = bind_inputs(
                    task,
                    dep_tasks=self._dep_map(),
                    dep_outputs=self.dep_outputs,
                    dep_provenance=self.dep_provenance,
                    known_claim_ids=known_claims,
                    claim_provenance=claim_provenance,
                    verdict_snapshots=verdict_snapshots,
                )
        except InputUnavailable as e:
            kids = children_map
            reason = str(e.reason)
            if any(i.from_task in kids and not kids[i.from_task] for i in task.spec.inputs):
                detail = "foreach_empty"
            elif reason == "foreach_quorum_gt_children":
                detail = "foreach_quorum_gt_children"
            elif reason == "quorum" or any(i.quorum is not None for i in task.spec.inputs):
                detail = "quorum"
            else:
                detail = None
            await self.cancel_task(
                task.task_id,
                reason=TaskReason.INPUT_UNAVAILABLE,
                detail=detail,
            )
            raise

        # method=simulation is scientific metadata (not a stub by itself); input
        # provenance from bind_inputs still downgrades (H-b / DESIGN §6.7).
        prov = provenance if provenance is not None else derived
        if prov not in VALID_PROVENANCE:
            raise ValueError(f"invalid provenance: {prov}")

        # PR18: taint is computed in core from input artifact metas (never caller).
        from coscientist.core.execution.taint import bindings_tainted

        arts = getattr(self, "artifacts", None) or getattr(
            getattr(self, "executor", None), "artifacts", None
        )
        get_meta = getattr(arts, "get_meta", None) if arts is not None else None
        tainted_context = bindings_tainted(bindings, get_meta)

        aid = attempt_id(task.task_id, n)
        now = self.clock.now()
        assert self.study is not None
        sid = self.study.study_id
        from coscientist.core.events.schema_versions import ATTEMPT_STARTED_SCHEMA_VERSION
        from coscientist.core.execution.domain import Limitation as _Limitation
        from coscientist.core.execution.input_limits import (
            apply_input_drift,
            inputs_digest_of,
            previous_attempt_inputs_state,
            snapshot_bindings_at_p2,
            study_user_input_bindings,
        )
        from coscientist.core.plan.shares import attempt_share_fields_or_refuse

        # PR19: bind Study.user_input_ids so DAG tasks consume them + drift covers.
        user_ids = list(getattr(self.study, "user_input_ids", None) or [])
        if user_ids:
            if get_meta is None:
                # Fail closed: cannot bind user inputs without a store.
                limitations = list(limitations) + [
                    _Limitation(text="input_digest_unavailable", ref="user_input_ids")
                ]
                prov = worst_provenance(prov, "partial")
            else:
                bindings = list(
                    study_user_input_bindings(
                        user_input_ids=user_ids,
                        now=now,
                        get_meta=get_meta,
                    )
                ) + list(bindings)

        # PR19: P2 content snapshot + inputs_digest; drift → limitation + partial.
        # get_meta errors propagate (never silently None).
        bindings, snap_lims = snapshot_bindings_at_p2(
            bindings, now=now, get_meta=get_meta
        )
        limitations = list(limitations) + list(snap_lims)
        if snap_lims:
            prov = worst_provenance(prov, "partial")
        in_digest = inputs_digest_of(bindings)
        prev_digest, prev_bindings = previous_attempt_inputs_state(
            self.attempts, list(task.attempts), exclude_aid=aid
        )
        prov, limitations = apply_input_drift(
            provenance=prov,
            limitations=limitations,
            inputs_digest=in_digest,
            previous_inputs_digest=prev_digest,
            previous_bindings=prev_bindings,
        )

        lim_payload = [lim.model_dump(mode="json") if hasattr(lim, "model_dump") else dict(lim) for lim in limitations]
        share_fields = await attempt_share_fields_or_refuse(self, task)
        if share_fields is None:
            # Zero share / empty pool: task already SKIPPED(budget); no AttemptStarted.
            return None
        payload = {
            "attempt_id": aid,
            "n": n,
            "dispatch_id": f"dispatch:{aid}",
            "route_returned": False,
            "closed": False,
            # Open attempts are not charged; close/result overwrites with True/False.
            "charged": False,
            "close_reason": None,
            "provenance": prov,
            "agent_output_digest": None,
            "task_id": task.task_id,
            "study_id": sid,
            "inputs": [b.model_dump(mode="json") for b in bindings],
            # PR19: sha256(JCS(sorted bindings)) beside config_digest (R14).
            "inputs_digest": in_digest,
            # PR18: core-computed taint for tool gating / derived artifacts.
            "tainted_context": tainted_context,
            # PR10: fan-in gaps (inputs below quorum / failed deps).
            "limitations": lim_payload,
            # PR20: attempt_role from journal ReviewScheduled.reviewer_task_id
            # (never a start_attempt kwarg).
            "attempt_role": _attempt_role_from_reviews(self, task.task_id),
            **share_fields,
        }
        events: list[EventEnvelope] = [
            EventEnvelope(
                study_id=sid,
                type="AttemptStarted",
                entity_key=aid,
                idem_key=make_idem("AttemptStarted", aid),
                payload=payload,
                ts=now,
                # AttemptStarted schema_version — see ATTEMPT_STARTED_SCHEMA_VERSION.
                schema_version=ATTEMPT_STARTED_SCHEMA_VERSION,
            )
        ]
        # Persist full verdict snapshots used at bind (not digest-only).
        bound_verdicts = {
            b.ref: verdict_snapshots[b.ref]
            for b in bindings
            if b.kind == "verdict" and b.ref and b.ref in verdict_snapshots
        }
        if bound_verdicts:
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="VerdictSnapshotsRecorded",
                    entity_key=f"{sid}:verdicts:{aid}",
                    idem_key=make_idem("VerdictSnapshotsRecorded", aid),
                    payload={
                        "study_id": sid,
                        "attempt_id": aid,
                        "scope": "bind",
                        "snapshots": bound_verdicts,
                    },
                    ts=now,
                    schema_version=1,
                )
            )
        # PR7: uses_claim edges for kind=claim inputs (DESIGN_M1 §3).
        events.extend(
            uses_claim_graph_events(
                study_id=sid,
                task=task,
                bindings=bindings,
                now=now,
                graph=graph,
            )
        )
        await self._append(events)
        return aid

    async def fail_attempt(
        self,
        aid: str,
        *,
        retryable: bool = True,
        close_reason: str | CloseReason = CloseReason.FAILED,
    ) -> None:
        att = self.attempts[aid]
        if _attempt_closed(att, ctx=aid):
            raise RuntimeError("attempt closed")
        now = self.clock.now()
        sid = att["study_id"]
        tid = att["task_id"]
        task = self.tasks[tid]
        cr = coerce_close_reason(close_reason)
        assert cr is not None
        cr_s = cr.value
        # fail_attempt always charges (crash/recover path; not sandbox infra).
        closing_charged = True
        events = [
            EventEnvelope(
                study_id=sid,
                type="AttemptClosed",
                entity_key=aid,
                idem_key=make_idem("AttemptClosed", aid, cr_s),
                payload={
                    "attempt_id": aid,
                    "closed": True,
                    "close_reason": cr_s,
                    "retryable": retryable,
                    "task_id": tid,
                    "charged": closing_charged,
                },
                ts=now,
                schema_version=2,
            )
        ]
        # Budget uses charged attempts (required on each closed attempt row).
        charged = 0
        for a in task.attempts:
            if a == aid:
                if closing_charged:
                    charged += 1
                continue
            row = self.attempts[a]
            if _attempt_charged(row, ctx=a):
                charged += 1
        can_retry = (
            retryable and charged < self.max_attempts and now < task.deadline
        )
        if not can_retry:
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="TaskDone",
                    entity_key=tid,
                    idem_key=make_idem("TaskDone", tid),
                    payload={
                        "task_id": tid,
                        "outcome": TaskOutcome.FAILED.value,
                        "reason": TaskReason.FAILED.value,
                    },
                    ts=now,
                    schema_version=2,
                )
            )
        else:
            # P3: leave PENDING with retry_at — never RUNNING without an open attempt.
            events[0] = events[0].model_copy(
                update={
                    "payload": {
                        **events[0].payload,
                        "retry_at": (now + timedelta(seconds=RETRY_BACKOFF_S)).isoformat(),
                    }
                }
            )
        await self._append(events)
        # P3: retry_at is persisted on AttemptClosed and folded into ``_retry_at``;
        # ``tick()`` starts the next attempt when ``clock.now() >= retry_at``.

    def _provenance_for_retry(self, tid: str) -> Provenance:
        """Last closed attempt's provenance (survives crash/reopen via the journal)."""
        task = self.tasks.get(tid)
        if task is None:
            raise ValueError(f"unknown task for retry provenance: {tid!r}")
        for aid in reversed(task.attempts):
            att = self.attempts[aid]
            if _attempt_closed(att, ctx=aid):
                prov = require_journal_field(att, "provenance", ctx=aid)
                if prov not in VALID_PROVENANCE:
                    raise ValueError(f"invalid closed attempt provenance: {prov!r}")
                return prov  # type: ignore[return-value]
        raise ValueError(f"no closed attempt provenance for task: {tid!r}")

    async def cancel_task(
        self,
        task_key_or_id: str,
        reason: str | TaskReason = TaskReason.CANCELLED,
        *,
        detail: str | None = None,
    ) -> None:
        task = self._resolve_task(task_key_or_id)
        if task.state == TaskState.DONE:
            return
        now = self.clock.now()
        assert self.study is not None
        tr = coerce_task_reason(reason)
        assert tr is not None
        reason_s = tr.value
        cr_s = _close_reason_for_cancel(reason_s)
        events: list[EventEnvelope] = []
        for aid in task.attempts:
            if not _attempt_closed(self.attempts[aid], ctx=aid):
                events.append(
                    EventEnvelope(
                        study_id=self.study.study_id,
                        type="AttemptClosed",
                        entity_key=aid,
                        idem_key=make_idem("AttemptClosed", aid, cr_s),
                        payload={
                            "attempt_id": aid,
                            "closed": True,
                            "close_reason": cr_s,
                            "task_id": task.task_id,
                            "charged": True,
                        },
                        ts=now,
                        schema_version=2,
                    )
                )
                t = self._attempt_tasks.pop(aid, None)
                if t:
                    t.cancel()
        done_payload: dict[str, Any] = {
            "task_id": task.task_id,
            "outcome": TaskOutcome.SKIPPED.value,
            "reason": reason_s,
        }
        if detail:
            done_payload["detail"] = detail
        events.append(
            EventEnvelope(
                study_id=self.study.study_id,
                type="TaskDone",
                entity_key=task.task_id,
                idem_key=make_idem("TaskDone", task.task_id),
                payload=done_payload,
                ts=now,
                schema_version=2,
            )
        )
        await self._append(events)
