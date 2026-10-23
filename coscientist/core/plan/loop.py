"""LoopSpec materialization (M1 PR11 / S10): PlanExpanded(loop=true) + LoopCapped.

Core-driven (like foreach): no planner, no expansion quota. ``max_iter`` is
mandatory; at the cap emit ``LoopCapped`` (not study terminal_reason). Totality:
exceptions become refuse-style report lines — never crash finalize.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import TYPE_CHECKING, Any

from coscientist.checks.sandbox_limits import freeze_task_sandbox_limits
from coscientist.core.claims.reasons import CloseReason, TaskReason
from coscientist.core.claims.rules import PlanInvalid
from coscientist.core.events.errors import LeaseLost
from coscientist.core.events.events import (
    EventEnvelope,
    make_idem,
)
from coscientist.core.events.ids import task_id
from coscientist.core.events.schema_versions import (
    EXPANSION_REFUSED_SCHEMA_VERSION,
    LOOP_CAPPED_SCHEMA_VERSION,
    LOOP_COMPLETED_SCHEMA_VERSION,
    LOOP_REFUSED_SCHEMA_VERSION,
    PLAN_EXPANDED_SCHEMA_VERSION,
    TASK_CREATED_SCHEMA_VERSION,
)
from coscientist.core.execution.domain import (
    LoopSpec,
    Plan,
    QuestionSpec,
    Task,
    TaskKind,
    TaskSpec,
    TaskState,
)
from coscientist.core.plan.expand_graph import plan_graph_events, question_graph_events
from coscientist.core.plan.expand_validate import active_plan_blob, assign_core_keys
from coscientist.core.plan.loop_predicates import (
    LoopPredicateEvalError,
    UnknownLoopPredicateError,
    count_survivors,
    eval_loop_predicate,
    validate_loop_predicate,
)
from coscientist.core.plan.shares import critic_plan_shares

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime

LOOP_DEP_PREFIX = "loop:"
LOOP_REASON_ITERATION_CAP = "iteration_cap"
LOOP_REASON_ERROR = "error"
LOOP_REASON_REFUSED = "refused"

def normalize_loop_id(raw: str) -> str:
    """NFKC-normalize loop_id; reject empty / invisible-only ids."""
    s = unicodedata.normalize("NFKC", str(raw or ""))
    if not s or not s.strip():
        raise PlanInvalid("LoopSpec.loop_id required")
    # Reject format/control chars (Cf/Cc/Cs) that survive strip.
    if any(unicodedata.category(ch) in {"Cf", "Cc", "Cs"} for ch in s):
        raise PlanInvalid(f"LoopSpec.loop_id has invisible/control chars: {raw!r}")
    return s.strip()

def loop_dep_key(loop_id: str) -> str:
    return f"{LOOP_DEP_PREFIX}{loop_id}"

def is_loop_dep(key: str) -> bool:
    return str(key).startswith(LOOP_DEP_PREFIX)

def parse_loop_dep(key: str) -> str | None:
    """Return NFKC-normalized loop_id from ``loop:<id>``, or None if not a barrier."""
    if not is_loop_dep(key):
        return None
    raw = str(key)[len(LOOP_DEP_PREFIX) :]
    try:
        return normalize_loop_id(raw)
    except PlanInvalid:
        return None

def loop_plan_id(study_id: str, loop_id: str, iter_n: int, rev: int) -> str:
    return f"pl_loop_{study_id}_{loop_id}_i{iter_n}_r{rev}"

def _loops_from_plans(runtime: StudyRuntime) -> list[LoopSpec]:
    """Active LoopSpecs from the latest plan blob that carries ``loops``.

    Invalid journal rows raise (fail-closed) — never silently drop a loop.
    """
    seen: dict[str, LoopSpec] = {}
    # Prefer highest revision that defines loops.
    plans = list(getattr(runtime, "plans", {}).values())
    plans.sort(key=lambda p: int(p.get("revision") or 0))
    for p in plans:
        raw = p.get("loops") or []
        if not raw:
            continue
        for item in raw:
            if isinstance(item, LoopSpec):
                ls = item
            else:
                ls = LoopSpec.model_validate(item)
            seen[ls.loop_id] = ls
    return list(seen.values())

def _loop_state(runtime: StudyRuntime) -> dict[str, dict[str, Any]]:
    st = getattr(runtime, "_loop_state", None)
    if not isinstance(st, dict):
        runtime._loop_state = {}  # noqa: SLF001
        st = runtime._loop_state  # noqa: SLF001
    return st

def loop_status(runtime: StudyRuntime, loop_id: str) -> str | None:
    """None | running | done | capped."""
    row = _loop_state(runtime).get(loop_id)
    if not row:
        return None
    return str(row.get("status") or "") or None

def loop_is_complete(runtime: StudyRuntime, loop_id: str) -> bool:
    return loop_status(runtime, loop_id) in ("done", "capped", "refused")

def iter_keys(runtime: StudyRuntime, loop_id: str, iter_n: int) -> list[str]:
    row = _loop_state(runtime).get(loop_id) or {}
    by_iter = row.get("iters") or {}
    keys = by_iter.get(str(iter_n)) or by_iter.get(iter_n) or []
    return [str(k) for k in keys]

def current_iter(runtime: StudyRuntime, loop_id: str) -> int:
    row = _loop_state(runtime).get(loop_id) or {}
    try:
        return int(row.get("iter") or 0)
    except (TypeError, ValueError):
        return 0

def validate_loop_specs(
    loops: list[LoopSpec] | list[dict[str, Any]] | None,
    *,
    known_keys: set[str],
    max_loop_iter: int,
    known_questions: set[str] | None = None,
    known_hypotheses: set[str] | None = None,
) -> list[LoopSpec]:
    """Validate LoopSpecs; raise ``PlanInvalid`` (accept_plan → PlanRefused).

    Returns normalized ``LoopSpec`` list (NFKC loop_id). ``known_questions`` is
    always enforced when provided (including empty set). ``known_hypotheses``
    enforced when provided (including empty).
    """
    if not loops:
        return []
    qids = known_questions  # None = skip; set (even empty) = enforce
    hyps = known_hypotheses
    seen_ids: set[str] = set()
    out: list[LoopSpec] = []
    stage = {"SPEC", "HYP", "LIT", "PLAN", "REPORT"}
    for raw in loops:
        if isinstance(raw, dict):
            if "max_iter" not in raw or raw.get("max_iter") is None:
                raise PlanInvalid(
                    "LoopSpec.max_iter is required "
                    "(honest refusal: unbounded loops forbidden)"
                )
            try:
                ls = LoopSpec.model_validate(raw)
            except PlanInvalid:
                raise
            except Exception as e:
                raise PlanInvalid(f"invalid LoopSpec: {e}") from e
        else:
            ls = raw
        lid = normalize_loop_id(ls.loop_id)
        if lid != ls.loop_id:
            ls = ls.model_copy(update={"loop_id": lid})
        if lid in seen_ids:
            raise PlanInvalid(f"duplicate loop_id: {lid!r}")
        seen_ids.add(lid)
        if int(ls.max_iter) < 1:
            raise PlanInvalid(f"LoopSpec.max_iter must be >= 1 (loop {lid!r})")
        if int(ls.max_iter) > int(max_loop_iter):
            # Do not interpolate max_iter values — huge ints blow f-strings.
            raise PlanInvalid(
                f"LoopSpec.max_iter > study.max_loop_iter (loop {lid!r})"
            )
        if not ls.body:
            raise PlanInvalid(f"LoopSpec.body empty (loop {lid!r})")
        validate_loop_predicate(ls.until)
        body_keys = {t.task_key for t in ls.body}
        if len(body_keys) != len(ls.body):
            raise PlanInvalid(f"LoopSpec.body duplicate task_key (loop {lid!r})")
        for t in ls.body:
            if t.foreach is not None:
                raise PlanInvalid(
                    f"loop body task {t.task_key!r} must not set foreach "
                    f"(loop {lid!r})"
                )
            if t.loop_id is not None or t.loop_iter is not None:
                raise PlanInvalid(
                    f"loop body template {t.task_key!r} must not set loop_id/loop_iter"
                )
            for d in t.depends_on:
                if d.startswith(LOOP_DEP_PREFIX):
                    continue
                if d not in body_keys and d not in known_keys and d not in stage:
                    raise PlanInvalid(
                        f"loop body {t.task_key!r} depends_on unknown {d!r} "
                        f"(loop {lid!r})"
                    )
        for d in ls.depends_on:
            if d not in known_keys and d not in stage:
                raise PlanInvalid(
                    f"LoopSpec.depends_on unknown task {d!r} (loop {lid!r})"
                )
        if qids is not None:
            for qid in ls.scope_questions:
                if qid not in qids:
                    raise PlanInvalid(
                        f"LoopSpec.scope_questions unknown question {qid!r} "
                        f"(loop {lid!r})"
                    )
        norm_hyps: list[str] = []
        for hid in ls.scope_hypotheses:
            hid_n = unicodedata.normalize("NFKC", str(hid or "")).strip()
            if not hid_n:
                raise PlanInvalid(
                    f"LoopSpec.scope_hypotheses empty id (loop {lid!r})"
                )
            if any(unicodedata.category(ch) in {"Cf", "Cc", "Cs"} for ch in hid_n):
                raise PlanInvalid(
                    f"LoopSpec.scope_hypotheses invisible chars {hid!r} (loop {lid!r})"
                )
            if hyps is not None and hid_n not in hyps:
                raise PlanInvalid(
                    f"LoopSpec.scope_hypotheses unknown hypothesis {hid!r} "
                    f"(loop {lid!r})"
                )
            norm_hyps.append(hid_n)
        if norm_hyps != list(ls.scope_hypotheses):
            ls = ls.model_copy(update={"scope_hypotheses": norm_hyps})
        out.append(ls)
    return out

def loop_known(runtime: StudyRuntime, loop_id: str) -> bool:
    """True when a LoopSpec with this id exists in accepted/expanded plans."""
    for ls in _loops_from_plans(runtime):
        if ls.loop_id == loop_id:
            return True
    return False

def _deps_ready(runtime: StudyRuntime, keys: list[str]) -> bool:
    for d in keys:
        dep_id = task_id(runtime.study.study_id, d) if runtime.study else d
        dep = runtime.tasks.get(dep_id)
        if dep is None:
            matches = [t for t in runtime.tasks.values() if t.spec.task_key == d]
            dep = matches[0] if matches else None
        if dep is None or dep.state != TaskState.DONE:
            return False
    return True

def _iter_all_done(runtime: StudyRuntime, loop_id: str, iter_n: int) -> bool:
    keys = iter_keys(runtime, loop_id, iter_n)
    if not keys:
        return False
    for k in keys:
        matches = [t for t in runtime.tasks.values() if t.spec.task_key == k]
        if not matches or matches[0].state != TaskState.DONE:
            return False
    return True

def _concrete_body(
    body: list[TaskSpec],
    *,
    loop_id: str,
    iter_n: int,
    key_map: dict[str, str],
    prev_keys: list[str],
) -> list[TaskSpec]:
    out: list[TaskSpec] = []
    for tmpl in body:
        new_key = key_map[tmpl.task_key]
        deps: list[str] = []
        for d in tmpl.depends_on:
            if d in key_map:
                deps.append(key_map[d])
            else:
                deps.append(d)
        # Sequential iters: every body task waits on previous iter barrier.
        if iter_n > 1 and prev_keys:
            for pk in prev_keys:
                if pk not in deps:
                    deps.append(pk)
        out.append(
            tmpl.model_copy(
                update={
                    "task_key": new_key,
                    "title": f"{tmpl.title}[{loop_id}#{iter_n}]",
                    "depends_on": deps,
                    "loop_id": loop_id,
                    "loop_iter": iter_n,
                    "loop_template_key": tmpl.task_key,
                    "foreach": None,
                    "foreach_item": None,
                }
            )
        )
    return out

async def materialize_loop_iter(
    runtime: StudyRuntime,
    loop: LoopSpec,
    *,
    iter_n: int,
) -> str | None:
    """Write PlanExpanded(loop=true) for one iteration. Returns plan_id or None."""
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return None
    study = runtime.study
    sid = study.study_id
    st = _loop_state(runtime)
    row = st.get(loop.loop_id) or {}
    if row.get("status") in ("done", "capped"):
        return None
    # Idempotent: already materialized this iter.
    existing = (row.get("iters") or {}).get(str(iter_n))
    if existing:
        return None

    now = runtime.clock.now()
    prev_keys = iter_keys(runtime, loop.loop_id, iter_n - 1) if iter_n > 1 else []
    prototypes = [
        t.model_copy(update={"task_key": f"TMP-LOOP-{i}"})
        for i, t in enumerate(loop.body)
    ]
    # Preserve template keys for remapping: assign then zip.
    tmpl_keys = [t.task_key for t in loop.body]
    keyed = assign_core_keys(runtime, prototypes)
    if isinstance(keyed, str):
        await _refuse_loop(runtime, loop, detail=f"loop_assign_keys:{keyed}")
        return None
    key_map = {tmpl_keys[i]: keyed[i].task_key for i in range(len(tmpl_keys))}
    added = _concrete_body(
        list(loop.body),
        loop_id=loop.loop_id,
        iter_n=iter_n,
        key_map=key_map,
        prev_keys=prev_keys,
    )

    # max_tasks guard
    max_tasks = int(getattr(study, "max_tasks", 64) or 64)
    existing_n = len(runtime.tasks)
    if existing_n + len(added) > max_tasks:
        await _refuse_loop(
            runtime,
            loop,
            detail=f"loop_max_tasks:{existing_n + len(added)}>{max_tasks}",
        )
        return None

    prev = active_plan_blob(runtime) or {}
    prev_rev = int(prev.get("revision") or 0)
    prev_pid = str(prev.get("plan_id") or "")
    new_rev = prev_rev + 1
    prev_tasks = [
        TaskSpec.model_validate(t) if isinstance(t, dict) else t
        for t in (prev.get("tasks") or [])
    ]
    prev_questions = [
        QuestionSpec.model_validate(q) if isinstance(q, dict) else q
        for q in (prev.get("questions") or [])
    ]
    prev_loops = list(prev.get("loops") or [])
    merged = list(prev_tasks) + added
    share_err = critic_plan_shares(merged, study=study)
    if share_err is not None:
        await _refuse_loop(runtime, loop, detail=f"loop_shares:{share_err}")
        return None

    pid = loop_plan_id(sid, loop.loop_id, iter_n, new_rev)
    plan = Plan(
        plan_id=pid,
        study_id=sid,
        revision=new_rev,
        hypothesis_id=None,
        tasks=merged,
        questions=list(prev_questions),
        expand_after=list(prev.get("expand_after") or []),
        loops=[
            LoopSpec.model_validate(x) if isinstance(x, dict) else x for x in prev_loops
        ]
        or [loop],
    )
    ed = study.effective_deadline()
    events: list[EventEnvelope] = []
    events.extend(
        plan_graph_events(
            runtime,
            sid=sid,
            pid=pid,
            prev_pid=prev_pid,
            new_rev=new_rev,
            prev_rev=prev_rev,
            origin="plan",
            delegated_by=None,
            now=now,
        )
    )
    events.extend(
        question_graph_events(
            runtime,
            sid=sid,
            pid=pid,
            added_questions=[],
            superseded_questions=[],
            known_texts={q.id: q.text for q in prev_questions},
            now=now,
        )
    )
    catalog = getattr(runtime, "catalog", None)
    existing_keys = {t.spec.task_key for t in runtime.tasks.values()}
    for spec in added:
        if spec.task_key in existing_keys:
            continue
        frozen_spec = freeze_task_sandbox_limits(spec, catalog)
        tid = task_id(sid, frozen_spec.task_key)
        route = (frozen_spec.route or "").lower()
        kind = {
            "literature": TaskKind.LITERATURE,
            "lit": TaskKind.LITERATURE,
            "hypotheses": TaskKind.HYPOTHESES,
            "hypothesis": TaskKind.HYPOTHESES,
            "hyp": TaskKind.HYPOTHESES,
            "planner": TaskKind.PLAN,
            "plan": TaskKind.PLAN,
        }.get(route, TaskKind.EXPERIMENT)
        task = Task(
            task_id=tid,
            kind=kind,
            spec=frozen_spec,
            state=TaskState.PENDING,
            deadline=ed,
        )
        events.append(
            EventEnvelope(
                study_id=sid,
                type="TaskCreated",
                entity_key=tid,
                idem_key=make_idem("TaskCreated", tid, pid),
                payload=task.model_dump(mode="json"),
                ts=now,
                schema_version=TASK_CREATED_SCHEMA_VERSION,
            )
        )

    payload = {
        **plan.model_dump(mode="json"),
        "after_task": loop.depends_on[0] if loop.depends_on else None,
        "plan_rev": new_rev,
        "added_tasks": [t.model_dump(mode="json") for t in added],
        "added_questions": [],
        "superseded_tasks": [],
        "superseded_questions": [],
        "origin": "plan",
        "reason": f"loop:{loop.loop_id}:iter:{iter_n}",
        "delegated_by": None,
        "replan_allocated_s": 0.0,
        "prev_plan_id": prev_pid or None,
        "hitl_request_id": None,
        "plan_key": None,
        "foreach": False,
        "loop": True,
        "loop_id": loop.loop_id,
        "loop_iter": iter_n,
        "loop_max_iter": int(loop.max_iter),
        "loop_until": loop.until.model_dump(mode="json"),
        "loop_template_keys": list(tmpl_keys),
    }
    events.append(
        EventEnvelope(
            study_id=sid,
            type="PlanExpanded",
            entity_key=pid,
            idem_key=make_idem("PlanExpanded", "loop", sid, loop.loop_id, str(iter_n)),
            payload=payload,
            ts=now,
            schema_version=PLAN_EXPANDED_SCHEMA_VERSION,
        )
    )
    await runtime._append(events)  # noqa: SLF001
    return pid

async def _emit_loop_capped(
    runtime: StudyRuntime,
    loop: LoopSpec,
    *,
    iter_n: int,
    reason: str = LOOP_REASON_ITERATION_CAP,
    detail: str | None = None,
    survivors: int | None = None,
) -> None:
    """Journal LoopCapped. Never recomputes survivors on error/refuse paths."""
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return
    sid = runtime.study.study_id
    capped = getattr(runtime, "_loop_capped", None) or {}
    if isinstance(capped, dict) and loop.loop_id in capped:
        return
    status = loop_status(runtime, loop.loop_id)
    # LoopRefused folds status=refused *before* LoopCapped on the fail-closed
    # path — still emit LoopCapped(reason=error|refused) so reopen can restore
    # ``_loop_capped`` without re-eval. Skip only true prior terminals.
    if status in ("done", "capped"):
        return
    if survivors is None and reason == LOOP_REASON_ITERATION_CAP:
        survivors = count_survivors(runtime, loop, upto_iter=iter_n)
    now = runtime.clock.now()
    payload: dict[str, Any] = {
        "loop_id": loop.loop_id,
        "iter": iter_n,
        "max_iter": int(loop.max_iter),
        "survivors": survivors,
        "scope_hypotheses": list(loop.scope_hypotheses),
        "scope_questions": list(loop.scope_questions),
        "until": loop.until.model_dump(mode="json"),
        "reason": reason,
    }
    if detail:
        payload["detail"] = detail[:200]
        payload["refuse_detail"] = detail[:200]  # legacy report readers
    ev = EventEnvelope(
        study_id=sid,
        type="LoopCapped",
        entity_key=f"{sid}:loop:{loop.loop_id}",
        idem_key=make_idem("LoopCapped", sid, loop.loop_id, reason),
        payload=payload,
        ts=now,
        schema_version=LOOP_CAPPED_SCHEMA_VERSION,
    )
    await runtime._append([ev])  # noqa: SLF001

async def _emit_loop_refused(
    runtime: StudyRuntime,
    loop: LoopSpec,
    *,
    iter_n: int,
    detail: str,
    reason: str = LOOP_REASON_ERROR,
) -> None:
    """Journal LoopRefused + LoopCapped + ExpansionRefused in one append batch.

    Single batch avoids mid-emit ``status=refused`` early-outs that would skip
    LoopCapped (live ≡ replay needs ``_loop_capped`` populated).
    """
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return
    if loop_is_complete(runtime, loop.loop_id):
        return
    capped = getattr(runtime, "_loop_capped", None) or {}
    if isinstance(capped, dict) and loop.loop_id in capped:
        return
    sid = runtime.study.study_id
    now = runtime.clock.now()
    detail_s = detail[:200]
    after = loop.depends_on[0] if loop.depends_on else loop.loop_id
    until = loop.until.model_dump(mode="json")
    scope_h = list(loop.scope_hypotheses)
    scope_q = list(loop.scope_questions)
    max_i = int(loop.max_iter)
    events = [
        EventEnvelope(
            study_id=sid,
            type="LoopRefused",
            entity_key=f"{sid}:loop:{loop.loop_id}",
            idem_key=make_idem("LoopRefused", sid, loop.loop_id, reason),
            payload={
                "loop_id": loop.loop_id,
                "iter": iter_n,
                "max_iter": max_i,
                "reason": reason,
                "detail": detail_s,
                "scope_hypotheses": scope_h,
                "scope_questions": scope_q,
                "until": until,
            },
            ts=now,
            schema_version=LOOP_REFUSED_SCHEMA_VERSION,
        ),
        EventEnvelope(
            study_id=sid,
            type="LoopCapped",
            entity_key=f"{sid}:loop:{loop.loop_id}",
            idem_key=make_idem("LoopCapped", sid, loop.loop_id, reason),
            payload={
                "loop_id": loop.loop_id,
                "iter": iter_n,
                "max_iter": max_i,
                "survivors": None,  # never recompute — predicate may be what failed
                "scope_hypotheses": scope_h,
                "scope_questions": scope_q,
                "until": until,
                "reason": reason,
                "detail": detail_s,
                "refuse_detail": detail_s,
            },
            ts=now,
            schema_version=LOOP_CAPPED_SCHEMA_VERSION,
        ),
        EventEnvelope(
            study_id=sid,
            type="ExpansionRefused",
            entity_key=f"{sid}:loop:{loop.loop_id}:{reason}",
            idem_key=make_idem(
                "ExpansionRefused", sid, f"loop:{loop.loop_id}", reason, detail_s
            ),
            payload={
                "after_task": after,
                "reason": "invalid",
                "detail": detail_s,
                "plan_key": None,
                "loop_id": loop.loop_id,
                "loop": True,
                "loop_reason": reason,
            },
            ts=now,
            schema_version=EXPANSION_REFUSED_SCHEMA_VERSION,
        ),
    ]
    await runtime._append(events)  # noqa: SLF001

async def _emit_loop_completed(
    runtime: StudyRuntime, loop: LoopSpec, *, iter_n: int
) -> None:
    """Journal until-met completion so reopen restores status without re-eval."""
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return
    done_map = getattr(runtime, "_loop_done", None) or set()
    if isinstance(done_map, set) and loop.loop_id in done_map:
        return
    if loop_status(runtime, loop.loop_id) == "done":
        return
    sid = runtime.study.study_id
    survivors = count_survivors(runtime, loop, upto_iter=iter_n)
    now = runtime.clock.now()
    payload = {
        "loop_id": loop.loop_id,
        "iter": iter_n,
        "max_iter": int(loop.max_iter),
        "survivors": survivors,
        "scope_hypotheses": list(loop.scope_hypotheses),
        "scope_questions": list(loop.scope_questions),
        "until": loop.until.model_dump(mode="json"),
        "reason": "until_met",
    }
    ev = EventEnvelope(
        study_id=sid,
        type="LoopCompleted",
        entity_key=f"{sid}:loop:{loop.loop_id}",
        idem_key=make_idem("LoopCompleted", sid, loop.loop_id),
        payload=payload,
        ts=now,
        schema_version=LOOP_COMPLETED_SCHEMA_VERSION,
    )
    await runtime._append([ev])  # noqa: SLF001

async def _refuse_loop(
    runtime: StudyRuntime,
    loop: LoopSpec,
    *,
    detail: str,
    reason: str = LOOP_REASON_REFUSED,
) -> None:
    """Journal LoopRefused + LoopCapped(reason≠iteration_cap) for live ≡ replay."""
    cur = current_iter(runtime, loop.loop_id) or 0
    await _emit_loop_refused(
        runtime, loop, iter_n=cur, detail=detail, reason=reason
    )

async def _emit_loop_advance_failed(
    runtime: StudyRuntime, *, detail: str
) -> None:
    """Journal ExpansionRefused when no LoopSpec context is available."""
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return
    sid = runtime.study.study_id
    now = runtime.clock.now()
    detail_s = detail[:200]
    await runtime._append(  # noqa: SLF001
        [
            EventEnvelope(
                study_id=sid,
                type="ExpansionRefused",
                entity_key=f"{sid}:loop_advance:error",
                idem_key=make_idem("ExpansionRefused", sid, "loop_advance", detail_s),
                payload={
                    "after_task": None,
                    "reason": "invalid",
                    "detail": detail_s,
                    "plan_key": None,
                    "loop": True,
                    "loop_reason": LOOP_REASON_ERROR,
                    "report_line": "не выполнено: внутренняя ошибка (loop)",
                },
                ts=now,
                schema_version=EXPANSION_REFUSED_SCHEMA_VERSION,
            )
        ]
    )

async def finalize_loop_internal_error(
    runtime: StudyRuntime,
    *,
    exc: BaseException,
    loop: LoopSpec | None = None,
) -> None:
    """Journal the unexpected loop error (never silent) and finalize study.

    Terminal reason is ``internal_error`` (public ``TerminalReason.INTERNAL_ERROR``,
    same as #21). Expected predicate failures use ``_refuse_loop`` instead.
    Closes any RUNNING tasks/attempts before finalize so the journal is honest.
    """
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return
    detail = f"loop_advance:{type(exc).__name__}:{exc}"[:200]
    # Suppress expand/foreach/loop hooks on nested _append(TaskDone) while closing.
    runtime._finalizing = True  # noqa: SLF001
    try:
        try:
            if loop is not None:
                await _emit_loop_refused(
                    runtime,
                    loop,
                    iter_n=current_iter(runtime, loop.loop_id) or 0,
                    detail=detail,
                    reason=LOOP_REASON_ERROR,
                )
            else:
                await _emit_loop_advance_failed(runtime, detail=detail)
        except LeaseLost:
            raise
        except Exception as journal_exc:  # noqa: BLE001 — still must finalize
            log.warning(
                "finalize_loop_internal_error journal failed: %s",
                journal_exc,
                exc_info=True,
            )
        # Close open work so RUNNING tasks/attempts do not survive StudyFinalized.
        try:
            from coscientist.core.plan.cancel import _close_open_work

            close_events = _close_open_work(
                runtime, reason=TaskReason.CORE_BUG, close=CloseReason.CRASHED
            )
            if close_events:
                await runtime._append(close_events)  # noqa: SLF001
        except LeaseLost:
            raise
        except Exception as close_exc:  # noqa: BLE001 — still must finalize
            log.warning(
                "finalize_loop_internal_error close_open_work failed: %s",
                close_exc,
                exc_info=True,
            )
        import coscientist.core.finalize.finalize as finalize_mod

        await finalize_mod.finalize(runtime, trigger="internal_error")
    finally:
        runtime._finalizing = False  # noqa: SLF001

async def refuse_orphaned_loop_barriers(runtime: StudyRuntime) -> int:
    """SKIPPED-refuse tasks waiting on unknown/dropped ``loop:<id>`` barriers.

    A dropped / never-accepted LoopSpec must not leave dependents PENDING forever.
    """
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return 0
    n = 0
    for task in list(runtime.tasks.values()):
        if task.state == TaskState.DONE:
            continue
        for d in task.spec.depends_on:
            if not is_loop_dep(d):
                continue
            lid = parse_loop_dep(d)
            if lid is None:
                continue
            if loop_is_complete(runtime, lid):
                continue
            if loop_known(runtime, lid):
                continue
            # Unknown loop id — refuse the waiter (honest, not a hang).
            cancel = getattr(runtime, "cancel_task", None)
            if callable(cancel):
                await cancel(
                    task.task_id,
                    reason=TaskReason.SCHEMA_INVALID,
                    detail=f"unknown loop barrier {d}",
                )
                n += 1
            break
    return n

async def _advance_one(runtime: StudyRuntime, loop: LoopSpec) -> str | None:
    """Start next iter or emit LoopCapped / LoopCompleted. Returns plan_id if any.

    Predicate evaluation exceptions propagate (fail-closed) to ``maybe_advance_loops``,
    which journals an honest refuse — never masked as ``until=False``.
    """
    if loop_is_complete(runtime, loop.loop_id):
        return None
    cur = current_iter(runtime, loop.loop_id)
    if cur == 0:
        if not _deps_ready(runtime, list(loop.depends_on)):
            return None
        return await materialize_loop_iter(runtime, loop, iter_n=1)

    if not _iter_all_done(runtime, loop.loop_id, cur):
        return None

    met = eval_loop_predicate(runtime, loop, iter_n=cur, ctx={})

    if met:
        await _emit_loop_completed(runtime, loop, iter_n=cur)
        return None

    if cur >= int(loop.max_iter):
        await _emit_loop_capped(runtime, loop, iter_n=cur)
        return None

    return await materialize_loop_iter(runtime, loop, iter_n=cur + 1)

async def maybe_advance_loops(runtime: StudyRuntime) -> list[str]:
    """Advance all loops that can take a step (start / next iter / cap / complete).

    Expected eval failures (unknown predicate) → LoopRefused + LoopCapped(reason=error)
    without finalizing the study. Any other exception → journal + finalize
    ``internal_error`` (never leave TaskDone without StudyFinalized; never
    disguise as iteration_cap). Does not re-raise after finalize.
    """
    done: list[str] = []
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return done
    try:
        await refuse_orphaned_loop_barriers(runtime)
        loops = _loops_from_plans(runtime)
    except LeaseLost:
        raise
    except Exception as e:  # noqa: BLE001 — journal + finalize, never hang
        await finalize_loop_internal_error(runtime, exc=e, loop=None)
        return done
    for loop in loops:
        if getattr(runtime, "_finalized", False):
            break
        try:
            pid = await _advance_one(runtime, loop)
            if pid:
                done.append(pid)
        except (LoopPredicateEvalError, UnknownLoopPredicateError) as e:
            try:
                await _refuse_loop(
                    runtime,
                    loop,
                    detail=f"loop_predicate:{type(e).__name__}:{e}"[:200],
                    reason=LOOP_REASON_ERROR,
                )
            except LeaseLost:
                raise
            except Exception as refuse_exc:  # noqa: BLE001 — refuse path must not hang
                await finalize_loop_internal_error(
                    runtime, exc=refuse_exc, loop=loop
                )
                break
        except LeaseLost:
            raise
        except Exception as e:  # noqa: BLE001 — unexpected: finalize study
            await finalize_loop_internal_error(runtime, exc=e, loop=loop)
            break
    return done

async def maybe_loop_after_task_done(
    runtime: StudyRuntime, task_id_full: str
) -> list[str]:
    """After any TaskDone, try to advance loops (deps ready or iter complete)."""
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return []
    return await maybe_advance_loops(runtime)

async def resume_pending_loops(runtime: StudyRuntime) -> int:
    """Recover: re-advance loops so live == replay.

    Unexpected errors are journaled + finalized inside ``maybe_advance_loops``;
    this wrapper also catch-finalizes so recover never aborts before ``tick()``.
    """
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return 0
    try:
        got = await maybe_advance_loops(runtime)
        return len(got)
    except LeaseLost:
        raise
    except Exception as e:  # noqa: BLE001 — belt-and-suspenders for recover
        await finalize_loop_internal_error(runtime, exc=e, loop=None)
        return 0

def fold_loop_plan_expanded(runtime: Any, payload: dict[str, Any]) -> None:
    """Rebuild ``_loop_state`` from a PlanExpanded(loop=true) payload."""
    if not payload.get("loop"):
        return
    lid = str(payload.get("loop_id") or "")
    if not lid:
        return
    try:
        iter_n = int(payload.get("loop_iter") or 0)
    except (TypeError, ValueError):
        return
    keys = [
        str(t.get("task_key") if isinstance(t, dict) else getattr(t, "task_key", ""))
        for t in (payload.get("added_tasks") or [])
    ]
    keys = [k for k in keys if k]
    st = _loop_state(runtime)
    row = dict(st.get(lid) or {})
    iters = dict(row.get("iters") or {})
    iters[str(iter_n)] = keys
    row["iters"] = iters
    row["iter"] = max(int(row.get("iter") or 0), iter_n)
    if row.get("status") not in ("done", "capped"):
        row["status"] = "running"
    st[lid] = row

def fold_loop_capped(runtime: Any, payload: dict[str, Any]) -> None:
    lid = str(payload.get("loop_id") or "")
    if not lid:
        return
    reason = str(payload.get("reason") or LOOP_REASON_ITERATION_CAP)
    st = _loop_state(runtime)
    row = dict(st.get(lid) or {})
    row["status"] = "refused" if reason in (LOOP_REASON_ERROR, LOOP_REASON_REFUSED) else "capped"
    try:
        row["iter"] = int(payload.get("iter") or row.get("iter") or 0)
    except (TypeError, ValueError):
        pass
    row["survivors"] = payload.get("survivors")
    row["reason"] = reason
    detail = payload.get("detail") or payload.get("refuse_detail")
    if detail:
        row["refuse_detail"] = detail
    st[lid] = row
    capped = getattr(runtime, "_loop_capped", None)
    if not isinstance(capped, dict):
        runtime._loop_capped = {}
        capped = runtime._loop_capped
    capped[lid] = dict(payload)

def fold_loop_refused(runtime: Any, payload: dict[str, Any]) -> None:
    """Rebuild refused status from LoopRefused (before LoopCapped on the same path)."""
    lid = str(payload.get("loop_id") or "")
    if not lid:
        return
    st = _loop_state(runtime)
    row = dict(st.get(lid) or {})
    if row.get("status") in ("done", "capped", "refused"):
        return
    row["status"] = "refused"
    row["reason"] = str(payload.get("reason") or LOOP_REASON_ERROR)
    try:
        row["iter"] = int(payload.get("iter") or row.get("iter") or 0)
    except (TypeError, ValueError):
        pass
    if payload.get("detail"):
        row["refuse_detail"] = str(payload.get("detail"))[:200]
    st[lid] = row

def fold_loop_completed(runtime: Any, payload: dict[str, Any]) -> None:
    """Rebuild until-met completion from journal (live ≡ replay)."""
    lid = str(payload.get("loop_id") or "")
    if not lid:
        return
    st = _loop_state(runtime)
    row = dict(st.get(lid) or {})
    row["status"] = "done"
    try:
        row["iter"] = int(payload.get("iter") or row.get("iter") or 0)
    except (TypeError, ValueError):
        pass
    row["survivors"] = payload.get("survivors")
    row["complete_reason"] = str(payload.get("reason") or "until_met")
    st[lid] = row
    done = getattr(runtime, "_loop_done", None)
    if not isinstance(done, set):
        runtime._loop_done = set()
        done = runtime._loop_done
    done.add(lid)

def fold_loop_expansion_refused(runtime: Any, payload: dict[str, Any]) -> None:
    """ExpansionRefused for a loop restores terminal status (barrier / no re-advance).

    Does not populate ``_loop_capped`` (LoopCapped's job).
    """
    if not payload.get("loop") and not payload.get("loop_id"):
        return
    lid = str(payload.get("loop_id") or "")
    if not lid:
        return
    st = _loop_state(runtime)
    row = dict(st.get(lid) or {})
    if row.get("status") in ("done", "capped", "refused"):
        return
    loop_reason = str(payload.get("loop_reason") or LOOP_REASON_REFUSED)
    row["status"] = (
        "refused"
        if loop_reason in (LOOP_REASON_ERROR, LOOP_REASON_REFUSED)
        else "capped"
    )
    row["reason"] = loop_reason
    row["refuse_detail"] = str(payload.get("detail") or "")[:200]
    st[lid] = row

def capped_scope_questions(runtime: Any) -> dict[str, dict[str, Any]]:
    """qid → LoopCapped payload only for true iteration_cap with ≥1 iter.

    Refuse/error terminals and zero-iter caps are excluded — they must not stamp
    disposition reason=iteration_cap. Post-#21 rebase: also respect public
    ``TerminalReason.INTERNAL_ERROR`` (never map error-path loops to cap).
    """
    capped = getattr(runtime, "_loop_capped", None) or {}
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(capped, dict):
        return out
    for _lid, p in capped.items():
        if not isinstance(p, dict):
            continue
        reason = str(p.get("reason") or LOOP_REASON_ITERATION_CAP)
        if reason != LOOP_REASON_ITERATION_CAP:
            continue
        try:
            iter_n = int(p.get("iter") or 0)
        except (TypeError, ValueError):
            iter_n = 0
        if iter_n < 1:
            continue
        for qid in p.get("scope_questions") or []:
            out[str(qid)] = p
    return out
