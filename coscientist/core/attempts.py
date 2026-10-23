"""Attempt start / fail / cancel / retry (extracted to keep machine.py ≤ 500)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from coscientist.core.claims import compute_verdict_snapshots
from coscientist.core.domain import (
    VALID_PROVENANCE,
    Provenance,
    Task,
    TaskKind,
    TaskOutcome,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.ids import attempt_id, task_id
from coscientist.core.inputs import (
    InputUnavailable,
    bind_inputs,
    collect_bindable_claims,
    uses_claim_graph_events,
)
from coscientist.core.quorum import bind_input_groups
from coscientist.core.reasons import CloseReason, TaskReason, coerce_close_reason, coerce_task_reason

# Persisted on AttemptClosed.retry_at; fired by StudyRuntime.tick vs injected Clock.
RETRY_BACKOFF_S = 0.05

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
        children_map = getattr(self, "_foreach_children", None) or {}
        for d in task.spec.depends_on:
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

    async def start_attempt(
        self,
        task_key_or_id: str,
        *,
        provenance: Provenance | None = None,
    ) -> str:
        task = self._resolve_task(task_key_or_id)
        # Foreach templates are materialization placeholders — never dispatched.
        if task.spec.foreach is not None:
            raise RuntimeError("foreach template cannot be started")
        if task.state == TaskState.DONE:
            raise RuntimeError("task already DONE")
        if not self._deps_done(task):
            raise RuntimeError("dependencies not DONE")
        open_atts = [a for a in task.attempts if not self.attempts.get(a, {}).get("closed")]
        if open_atts:
            raise RuntimeError("already has open attempt")
        n = len(task.attempts) + 1
        if n > self.max_attempts:
            raise RuntimeError("max_attempts exceeded")

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
        from coscientist.core.foreach import expand_input_groups

        # PR10: per-input groups (foreach children keep the original quorum).
        groups = expand_input_groups(self, list(task.spec.inputs))
        children_map = getattr(self, "_foreach_children", None) or {}
        use_groups = any(
            inp.from_task in children_map or inp.quorum is not None for inp, _ in groups
        )
        limitations: list[Any] = []
        try:
            if use_groups:
                bindings, derived, limitations = bind_input_groups(
                    groups,
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
            if any(i.from_task in kids and not kids[i.from_task] for i in task.spec.inputs):
                detail = "foreach_empty"
            elif str(e.reason) == "quorum" or any(i.quorum is not None for i in task.spec.inputs):
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

        aid = attempt_id(task.task_id, n)
        now = self.clock.now()
        assert self.study is not None
        sid = self.study.study_id
        deadline = min(task.deadline, self.study.effective_deadline())
        lim_payload = [lim.model_dump(mode="json") if hasattr(lim, "model_dump") else dict(lim) for lim in limitations]
        payload = {
            "attempt_id": aid,
            "n": n,
            "dispatch_id": f"dispatch:{aid}",
            "route_returned": False,
            "closed": False,
            "close_reason": None,
            "provenance": prov,
            "agent_output_digest": None,
            "deadline": deadline.isoformat(),
            "task_id": task.task_id,
            "study_id": sid,
            # PR7: P2 bindings (+ digest) live in the journal for replay.
            "inputs": [b.model_dump(mode="json") for b in bindings],
            # PR10: fan-in gaps (inputs below quorum / failed deps).
            "limitations": lim_payload,
        }
        events: list[EventEnvelope] = [
            EventEnvelope(
                study_id=sid,
                type="AttemptStarted",
                entity_key=aid,
                idem_key=make_idem("AttemptStarted", aid),
                payload=payload,
                ts=now,
                schema_version=4,
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
        if att.get("closed"):
            raise RuntimeError("attempt closed")
        now = self.clock.now()
        sid = att["study_id"]
        tid = att["task_id"]
        task = self.tasks[tid]
        cr = coerce_close_reason(close_reason)
        assert cr is not None
        cr_s = cr.value
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
                },
                ts=now,
                schema_version=2,
            )
        ]
        n = int(att["n"])
        can_retry = retryable and n < self.max_attempts and now < task.deadline
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
            return "real"
        for aid in reversed(task.attempts):
            att = self.attempts.get(aid, {})
            if att.get("closed"):
                prov = att.get("provenance", "real")
                if prov in VALID_PROVENANCE:
                    return prov  # type: ignore[return-value]
        return "real"

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
        # Attempt close uses CloseReason; TaskReason-only values (e.g. human) map to cancelled.
        try:
            cr_s = CloseReason(reason_s).value
        except ValueError:
            cr_s = CloseReason.CANCELLED.value
        events: list[EventEnvelope] = []
        for aid in task.attempts:
            if not self.attempts.get(aid, {}).get("closed"):
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
