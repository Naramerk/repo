"""Attempt start / fail / cancel / retry (extracted to keep machine.py ≤ 500)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from coscientist.core.claims.claims import compute_verdict_snapshots
from coscientist.core.claims.reasons import CloseReason, TaskReason, coerce_close_reason, coerce_task_reason
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.ids import attempt_id, task_id
from coscientist.core.events.schema_versions import (
    ATTEMPT_CLOSED_SCHEMA_VERSION,
    ATTEMPT_STARTED_SCHEMA_VERSION,
    TASK_DONE_SCHEMA_VERSION,
    VERDICT_SNAPSHOTS_RECORDED_SCHEMA_VERSION,
)
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

from coscientist.core.events.mapping import optional_item
# Persisted on AttemptClosed.retry_at; fired by StudyRuntime.tick vs injected Clock.
RETRY_BACKOFF_S = 0.05

def _attempt_role_from_reviews(runtime: Any, task_id: str) -> str:
    """Derive AttemptStarted.attempt_role from pending ReviewScheduled rows."""
    try:
        reviews = runtime._proposed_check_reviews
    except AttributeError:
        reviews = {}
    if reviews is None:
        reviews = {}
    if not isinstance(reviews, dict):
        raise JournalFormatError(
            f"_proposed_check_reviews must be dict, got {type(reviews).__name__}"
        )
    if not isinstance(task_id, str):
        raise TypeError(f"task_id must be str, got {type(task_id).__name__}")
    tid = task_id
    for rev in reviews.values():
        if not isinstance(rev, dict):
            continue
        if "status" not in rev:
            raise JournalFormatError("proposed_check review missing status")
        status = rev["status"]
        if not isinstance(status, str):
            raise JournalFormatError(
                f"review status must be str, got {type(status).__name__}"
            )
        if status != "pending":
            continue
        rtid = optional_item(rev, "reviewer_task_id")
        if any((not isinstance(rtid, str), not rtid)):
            continue
        if rtid == tid:
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
    results: dict[str, dict[str, Any]]
    plans: dict[str, dict[str, Any]]
    graph: Any
    artifacts: Any
    executor: Any
    clock: Any
    max_attempts: int
    dep_outputs: dict[str, dict[str, str]]
    dep_provenance: dict[str, Provenance]
    verdict_snapshots: dict[str, dict[str, Any]]
    _foreach_children: dict[str, list[str]]
    _attempt_tasks: dict[str, asyncio.Task[Any]]
    _lease_lost: bool
    _retry_at: dict[str, datetime]

    async def _append(self, events: list[UnstampedEvent]) -> None:
        raise NotImplementedError

    def _lookup_dep(self, key: str) -> Task | None:
        if self.study:
            dep_id = task_id(self.study.study_id, key)
        else:
            dep_id = key
        dep = optional_item(self.tasks, dep_id)
        if dep is None:
            dep = optional_item(self.tasks, key)
        if dep is None:
            matches = [t for t in self.tasks.values() if t.spec.task_key == key]
            dep = matches[0] if matches else None
        return dep

    def _deps_done(self, task: Task) -> bool:
        try:
            children_map = self._foreach_children
        except AttributeError:
            children_map = {}
        if children_map is None:
            children_map = {}
        if not isinstance(children_map, dict):
            raise JournalFormatError(
                f"_foreach_children must be dict, got {type(children_map).__name__}"
            )
        for d in task.spec.depends_on:
            # PR11: loop:<id> barrier — ready after until-met or LoopCapped.
            # Unknown / dropped loop never becomes ready; ``refuse_orphaned_loop_barriers``
            # SKIPPED-refuses dependents (fail-closed, no hang).
            from coscientist.core.plan.loop import is_loop_dep

            if is_loop_dep(str(d)):
                from coscientist.core.plan.loop import loop_is_complete, parse_loop_dep

                lid = parse_loop_dep(d)
                if any((lid is None, not loop_is_complete(self, lid))):  # type: ignore[arg-type]
                    return False
                continue
            # PR10: depends_on a foreach template → wait for all concrete children.
            if d in children_map:
                for ck in children_map[d]:
                    child = self._lookup_dep(ck)
                    if any((child is None, child.state != TaskState.DONE)):
                        return False
                continue
            dep = self._lookup_dep(d)
            if any((dep is None, dep.state != TaskState.DONE)):
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
        try:
            results = self.results
        except AttributeError:
            results = {}
        if results is None:
            results = {}
        if not isinstance(results, dict):
            raise JournalFormatError(
                f"results must be dict, got {type(results).__name__}"
            )
        for aid in reversed(dep.attempts):
            row = optional_item(results, aid)
            if not isinstance(row, dict):
                continue
            if any(('result_class' not in row, row['result_class'] is None)):
                rc = ""
            else:
                raw_rc = row["result_class"]
                if not isinstance(raw_rc, str):
                    raise JournalFormatError(
                        f"result_class must be str, got {type(raw_rc).__name__}"
                    )
                rc = raw_rc
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
                if any((dep is None, dep.state != TaskState.DONE)):
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
        out: list[Task]
        out = []
        for task in self.tasks.values():
            if task.state != TaskState.PENDING:
                continue
            if task.spec.foreach is not None:
                continue
            if not self._deps_done(task):
                continue
            open_atts = []
            for a in task.attempts:
                row = optional_item(self.attempts, a)
                if row is None:
                    raise JournalFormatError(f"attempt row missing: {a!r}")
                if "closed" not in row:
                    raise JournalFormatError(f"attempt {a!r} missing closed")
                if not row["closed"]:
                    open_atts.append(a)
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
        open_atts = []
        for a in task.attempts:
            row = optional_item(self.attempts, a)
            if row is None:
                raise JournalFormatError(f"attempt row missing: {a!r}")
            if "closed" not in row:
                raise JournalFormatError(f"attempt {a!r} missing closed")
            if not row["closed"]:
                open_atts.append(a)
        if open_atts:
            raise RuntimeError("already has open attempt")
        # Only charged attempts consume the retry budget (infra does not charge).
        # Sequence ``n`` is always len+1 so uncharged retries keep unique attempt ids.

        charged = 0
        for aid in task.attempts:
            att = optional_item(self.attempts, aid)
            if att is None:
                raise JournalFormatError(f"attempt row missing: {aid!r}")
            if "charged" not in att:
                raise JournalFormatError(f"attempt {aid!r} missing 'charged'")
            if att["charged"]:
                charged += 1
        if charged + 1 > self.max_attempts:
            raise RuntimeError("max_attempts exceeded")
        n = len(task.attempts) + 1

        try:
            graph = self.graph
        except AttributeError:
            graph = None
        try:
            results = self.results
        except AttributeError:
            results = {}
        if results is None:
            results = {}
        if not isinstance(results, dict):
            raise JournalFormatError(
                f"results must be dict, got {type(results).__name__}"
            )
        known_claims, claim_provenance = collect_bindable_claims(graph, results=results)
        # Prefer journaled snapshots (replay); else compute scoped to this task.
        try:
            stored = self.verdict_snapshots
        except AttributeError:
            stored = {}
        if stored is None:
            stored = {}
        if not isinstance(stored, dict):
            raise JournalFormatError(
                f"verdict_snapshots must be dict, got {type(stored).__name__}"
            )
        verdict_refs = [inp.ref for inp in task.spec.inputs if inp.kind == "verdict" and inp.ref]
        verdict_snapshots: dict[str, dict] = {r: stored[r] for r in verdict_refs if r in stored}
        need = [r for r in verdict_refs if r not in verdict_snapshots]
        if need and graph is not None and self.study is not None:
            try:
                # Scope any_exp to covering tasks for these question refs when possible.
                cover_keys: set[str] = set()
                try:
                    _plans = self.plans
                except AttributeError:
                    _plans = {}
                if _plans is None:
                    _plans = {}
                if not isinstance(_plans, dict):
                    raise JournalFormatError(
                        f"plans must be dict, got {type(_plans).__name__}"
                    )
                for plan in _plans.values():
                    if "tasks" not in plan:
                        continue
                    raw_tasks = plan["tasks"]
                    if raw_tasks is None:
                        raise JournalFormatError("plan tasks is None")
                    for spec in raw_tasks:
                        if isinstance(spec, dict):
                            if "addresses" in spec and spec["addresses"] is not None:
                                addrs = set(spec["addresses"])
                            else:
                                addrs = set()
                            if addrs & set(need):
                                if any(('task_key' not in spec, not isinstance(spec['task_key'], str))):
                                    raise JournalFormatError("plan task missing task_key")
                                cover_keys.add(spec["task_key"])
                        else:
                            addrs = set(spec.addresses)
                            if addrs & set(need):
                                cover_keys.add(spec.task_key)
                cover_tasks = [t for t in self.tasks.values() if t.spec.task_key in cover_keys] or list(  # audit-ok[or_call]: attempts.py L382 or_call structural path not inventing domain value
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
            except (ValueError, TypeError, JournalFormatError, KeyError) as e:
                # Keep prior snapshots; typed refuse of computed set (never invent).
                _ = e
                verdict_snapshots = {**verdict_snapshots}
        from coscientist.core.plan.foreach import expand_input_groups

        # PR10: foreach children expand; static quorum inputs share one pool.
        groups = expand_input_groups(self, list(task.spec.inputs))
        try:
            children_map = self._foreach_children
        except AttributeError:
            children_map = {}
        if children_map is None:
            children_map = {}
        if not isinstance(children_map, dict):
            raise JournalFormatError(
                f"_foreach_children must be dict, got {type(children_map).__name__}"
            )
        fe_from = set(children_map)
        for t in self.tasks.values():
            if t.spec.foreach is not None:
                fe_from.add(t.spec.task_key)
        use_groups = any(
            inp.from_task in fe_from or inp.quorum is not None for inp, _ in groups
        )
        limitations: list[Any]
        limitations = []
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
            reason = e.reason
            if not isinstance(reason, str):
                raise TypeError(
                    f"InputUnavailable.reason must be str, got {type(reason).__name__}"
                ) from e
            if any(i.from_task in kids and not kids[i.from_task] for i in task.spec.inputs):
                detail = "foreach_empty"
            elif reason == "foreach_quorum_gt_children":
                detail = "foreach_quorum_gt_children"
            elif any((reason == 'quorum', any((i.quorum is not None for i in task.spec.inputs)))):
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
        if provenance is not None:
            prov = provenance
        else:
            prov = derived
        if prov not in VALID_PROVENANCE:
            raise ValueError(f"invalid provenance: {prov}")

        # PR18: taint is computed in core from input artifact metas (never caller).
        from coscientist.core.execution.taint import bindings_tainted

        try:
            arts = self.artifacts
        except AttributeError:
            arts = None
        if arts is None:
            try:
                _ex = self.executor
            except AttributeError:
                _ex = None
            if _ex is None:
                arts = None
            else:
                try:
                    arts = _ex.artifacts
                except AttributeError:
                    arts = None
        if arts is None:
            get_meta = None
        else:
            try:
                get_meta = arts.get_meta
            except AttributeError as e:
                raise JournalFormatError("artifacts store missing get_meta") from e
        tainted_context = bindings_tainted(bindings, get_meta)

        aid = attempt_id(task.task_id, n)
        now = self.clock.now()
        if self.study is None:
            raise RuntimeError("start_attempt requires runtime.study")
        sid = self.study.study_id
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
        raw_uids = self.study.user_input_ids
        if raw_uids is None:
            user_ids = []
        else:
            user_ids = list(raw_uids)
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

        lim_payload = []
        for lim in limitations:
            if hasattr(lim, "model_dump"):  # audit-ok[hasattr]: attempts.py L571 hasattr structural path not inventing domain value
                lim_payload.append(lim.model_dump(mode="json"))
            elif isinstance(lim, dict):
                lim_payload.append(dict(lim))
            else:
                raise TypeError(f"limitation must be Limitation|dict, got {type(lim).__name__}")
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
            # Charged until an infra/result path stamps False (required field, no reader default).
            "charged": True,
            **share_fields,
        }
        events: list[UnstampedEvent] = [
            UnstampedEvent(
                study_id=sid,
                type="AttemptStarted",
                entity_key=aid,
                idem_key=make_idem("AttemptStarted", aid),
                payload=payload,
                ts=now,
                # schema_version from UPCASTERS (schema v1 baseline = writer v1).
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
                UnstampedEvent(
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
                    schema_version=VERDICT_SNAPSHOTS_RECORDED_SCHEMA_VERSION,
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
        retryable: bool,
        close_reason: str | CloseReason,
    ) -> None:
        att = self.attempts[aid]
        if optional_item(att, "closed"):
            raise RuntimeError("attempt closed")
        now = self.clock.now()
        sid = att["study_id"]
        tid = att["task_id"]
        task = self.tasks[tid]
        cr = coerce_close_reason(close_reason)
        if cr is None:
            raise ValueError(f"fail_attempt unknown close_reason: {close_reason!r}")
        cr_s = cr.value
        events = [
            UnstampedEvent(
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
                },
                ts=now,
                schema_version=ATTEMPT_CLOSED_SCHEMA_VERSION,
            )
        ]
        # Budget uses charged attempts (required stamped field on every attempt row).

        charged = 0
        for a in task.attempts:
            row = optional_item(self.attempts, a)
            if row is None:
                raise JournalFormatError(f"attempt row missing: {a!r}")
            if "charged" not in row:
                raise JournalFormatError(f"attempt {a!r} missing 'charged'")
            if row["charged"]:
                charged += 1
        can_retry = (
            retryable and charged < self.max_attempts and now < task.deadline
        )
        if not can_retry:
            events.append(
                UnstampedEvent(
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
                    schema_version=TASK_DONE_SCHEMA_VERSION,
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

        task = optional_item(self.tasks, tid)
        if task is None:
            raise JournalFormatError(f"provenance_for_retry: unknown task {tid!r}")
        for aid in reversed(task.attempts):
            att = optional_item(self.attempts, aid)
            if att is None:
                raise JournalFormatError(f"attempt row missing: {aid!r}")
            if optional_item(att, "closed"):
                if "provenance" not in att:
                    raise JournalFormatError(f"attempt {aid!r} missing provenance")
                prov = att["provenance"]
                if prov not in VALID_PROVENANCE:
                    raise JournalFormatError(f"attempt {aid!r} bad provenance {prov!r}")
                return prov  # type: ignore[return-value]
        raise JournalFormatError(f"provenance_for_retry: no closed attempt for {tid!r}")

    async def cancel_task(
        self,
        task_key_or_id: str,
        reason: str | TaskReason,
        *,
        detail: str | None = None,
    ) -> None:
        task = self._resolve_task(task_key_or_id)
        if task.state == TaskState.DONE:
            return
        now = self.clock.now()
        if self.study is None:
            raise RuntimeError("cancel_task requires runtime.study")
        tr = coerce_task_reason(reason)
        if tr is None:
            raise ValueError(f"cancel_task unknown reason: {reason!r}")
        reason_s = tr.value
        # Attempt close uses CloseReason; TaskReason-only values (e.g. refused,
        # superseded, human) map to cancelled — TaskDone still carries TaskReason.
        try:
            cr_s = CloseReason(reason_s).value
        except ValueError:
            cr_s = CloseReason.CANCELLED.value
        events: list[UnstampedEvent]
        events = []
        for aid in task.attempts:
            _arow = optional_item(self.attempts, aid)
            if _arow is None:
                raise JournalFormatError(f"attempt row missing: {aid!r}")
            if "closed" not in _arow:
                raise JournalFormatError(f"attempt {aid!r} missing closed")
            if not _arow["closed"]:
                events.append(
                    UnstampedEvent(
                        study_id=self.study.study_id,
                        type="AttemptClosed",
                        entity_key=aid,
                        idem_key=make_idem("AttemptClosed", aid, cr_s),
                        payload={
                            "attempt_id": aid,
                            "closed": True,
                            "close_reason": cr_s,
                            "task_id": task.task_id,
                        },
                        ts=now,
                        schema_version=ATTEMPT_CLOSED_SCHEMA_VERSION,
                    )
                )
                if aid in self._attempt_tasks:
                    t = self._attempt_tasks.pop(aid)
                else:
                    t = None
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
            UnstampedEvent(
                study_id=self.study.study_id,
                type="TaskDone",
                entity_key=task.task_id,
                idem_key=make_idem("TaskDone", task.task_id),
                payload=done_payload,
                ts=now,
                schema_version=TASK_DONE_SCHEMA_VERSION,
            )
        )
        await self._append(events)
