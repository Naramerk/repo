
def _coerce_model(cls, value):
    if isinstance(value, dict):
        return cls.model_validate(value)
    return value


"""LoopSpec materialization (M1 PR11 / S10): PlanExpanded(loop=true) + LoopCapped.

Core-driven (like foreach): no planner, no expansion quota. ``max_iter`` is
mandatory; at the cap emit ``LoopCapped`` (not study terminal_reason). Totality:
exceptions become refuse-style report lines — never crash finalize.
"""

from __future__ import annotations

import logging
import unicodedata
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from coscientist.checks.sandbox_limits import freeze_task_sandbox_limits
from coscientist.core.claims.reasons import CloseReason, TaskReason
from coscientist.core.claims.rules import PlanInvalid
from coscientist.core.events.errors import JournalFormatError, LeaseLost
from coscientist.core.events.events import (
    UnstampedEvent,
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
from coscientist.core.events.upcasters import require_exact_int
from coscientist.core.execution.domain import (
    LoopSpec,
    Plan,
    QuestionSpec,
    Task,
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

from coscientist.core.events.mapping import optional_item
log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime

LOOP_DEP_PREFIX = "loop:"
LOOP_REASON_ITERATION_CAP = "iteration_cap"
LOOP_REASON_ERROR = "error"
LOOP_REASON_REFUSED = "refused"
LOOP_CAPPED_REASONS = frozenset(
    {LOOP_REASON_ITERATION_CAP, LOOP_REASON_ERROR, LOOP_REASON_REFUSED}
)
LOOP_REFUSED_REASONS = frozenset({LOOP_REASON_ERROR, LOOP_REASON_REFUSED})
LOOP_COMPLETED_REASONS = frozenset({"until_met"})
DETAIL_MAX_LEN = 200



class LoopStatus(StrEnum):
    """Persisted loop row status — never free string literals at write sites."""

    RUNNING = "running"
    DONE = "done"
    CAPPED = "capped"
    REFUSED = "refused"


def _require_loop_reason(
    payload: dict[str, Any],
    *,
    key: str,
    allowed: frozenset[str],
    what: str,
) -> str:
    try:
        reason = payload[key]
    except KeyError as e:
        raise JournalFormatError(f"{what}: missing {key!r}") from e
    reason_s = str(reason)
    if reason_s not in allowed:
        raise JournalFormatError(f"{what}: unknown {key}={reason_s!r}")
    return reason_s


def _require_loop_iter(payload: dict[str, Any], *, what: str) -> int:
    try:
        raw = payload["iter"]
    except KeyError as e:
        raise JournalFormatError(f"{what}: missing 'iter'") from e
    return require_exact_int(raw, what=f"{what}.iter")

def normalize_loop_id(raw: str) -> str:
    """NFKC-normalize loop_id; reject empty / invisible-only ids."""
    if raw is None:
        raise PlanInvalid("LoopSpec.loop_id required")
    s = unicodedata.normalize("NFKC", str(raw))
    if any((not s, not s.strip())):
        raise PlanInvalid("LoopSpec.loop_id required")
    # Reject format/control chars (Cf/Cc/Cs) that survive strip.
    if any(unicodedata.category(ch) in {"Cf", "Cc", "Cs"} for ch in s):
        raise PlanInvalid(f"LoopSpec.loop_id has invisible/control chars: {raw!r}")
    return s.strip()

def loop_dep_key(loop_id: str) -> str:
    return f"{LOOP_DEP_PREFIX}{loop_id}"

def is_loop_dep(key: str) -> bool:
    if not isinstance(key, str):
        raise TypeError(f"depends_on key must be str, got {type(key).__name__}")
    return key[: len(LOOP_DEP_PREFIX)] == LOOP_DEP_PREFIX

def parse_loop_dep(key: str) -> str | None:
    """Return NFKC-normalized loop_id from ``loop:<id>``, or None if not a barrier.

    Malformed ``loop:`` keys raise ``PlanInvalid`` (fail-closed) — never swallow.
    """
    if not is_loop_dep(key):
        return None
    raw = str(key)[len(LOOP_DEP_PREFIX) :]
    return normalize_loop_id(raw)

def loop_plan_id(study_id: str, loop_id: str, iter_n: int, rev: int) -> str:
    return f"pl_loop_{study_id}_{loop_id}_i{iter_n}_r{rev}"

def _loops_from_plans(runtime: StudyRuntime) -> list[LoopSpec]:
    """Active LoopSpecs from the latest plan blob that carries ``loops``.

    Invalid journal rows raise (fail-closed) — never silently drop a loop.
    """
    seen: dict[str, LoopSpec]
    seen = {}
    # Prefer highest revision that defines loops.
    plans = list(runtime.plans.values())
    plans.sort(key=lambda p: require_exact_int(p["revision"], what="plan.revision"))
    for p in plans:
        if "loops" not in p:
            continue
        raw = p["loops"]
        if raw is None:
            raise JournalFormatError("plan.loops must not be null")
        if not isinstance(raw, list):
            raise JournalFormatError(
                f"plan.loops must be list, got {type(raw).__name__}"
            )
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
    st = runtime._loop_state
    if st is None:
        runtime._loop_state = {}  # noqa: SLF001
        return runtime._loop_state  # noqa: SLF001
    if not isinstance(st, dict):
        raise JournalFormatError(
            f"_loop_state must be dict, got {type(st).__name__}"
        )
    return st

def loop_status(runtime: StudyRuntime, loop_id: str) -> str | None:
    """None when loop row absent; otherwise require a non-empty status string."""
    row = optional_item(_loop_state(runtime), loop_id)
    if row is None:
        return None
    if not isinstance(row, dict):
        raise JournalFormatError(f"loop state must be dict, got {type(row).__name__}")
    if "status" not in row:
        raise JournalFormatError(f"loop.status missing for {loop_id!r}")
    st = row["status"]
    if any((not isinstance(st, str), not st)):
        raise JournalFormatError(f"loop.status invalid: {st!r}")
    return st

def loop_is_complete(runtime: StudyRuntime, loop_id: str) -> bool:
    return loop_status(runtime, loop_id) in ("done", "capped", "refused")

def iter_keys(runtime: StudyRuntime, loop_id: str, iter_n: int) -> list[str]:
    row = optional_item(_loop_state(runtime), loop_id)
    if row is None:
        raise JournalFormatError(f"loop.iters: unknown loop {loop_id!r}")
    if not isinstance(row, dict):
        raise JournalFormatError(f"loop state must be dict, got {type(row).__name__}")
    if "iters" not in row:
        raise JournalFormatError(f"loop.iters missing for {loop_id!r}")
    by_iter = row["iters"]
    if not isinstance(by_iter, dict):
        raise JournalFormatError(f"loop.iters must be dict, got {type(by_iter).__name__}")
    key = str(iter_n)
    if key not in by_iter:
        raise JournalFormatError(f"loop.iters[{key}] missing for {loop_id!r}")
    keys = by_iter[key]
    if not isinstance(keys, list):
        raise JournalFormatError(f"loop.iters[{key}] must be list")
    return [str(k) for k in keys]

def current_iter(runtime: StudyRuntime, loop_id: str) -> int:
    row = optional_item(_loop_state(runtime), loop_id)
    if row is None:
        return 0
    if not isinstance(row, dict):
        raise JournalFormatError(f"loop state must be dict, got {type(row).__name__}")
    if "iter" not in row:
        raise JournalFormatError(f"loop.iter missing for {loop_id!r}")
    return require_exact_int(row["iter"], what="loop.iter")

def _require_loop_capped(runtime: StudyRuntime) -> dict[str, Any]:
    capped = runtime._loop_capped
    if capped is None:
        raise JournalFormatError("_loop_capped missing")
    if not isinstance(capped, dict):
        raise JournalFormatError(
            f"_loop_capped must be dict, got {type(capped).__name__}"
        )
    return capped

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
    out: list[LoopSpec]
    out = []
    from coscientist.core.execution.domain import STAGE_DEP_KEYS as stage
    for raw in loops:
        if isinstance(raw, dict):
            if any(('max_iter' not in raw, optional_item(raw, 'max_iter') is None)):
                raise PlanInvalid(
                    "LoopSpec.max_iter is required "
                    "(honest refusal: unbounded loops forbidden)"
                )
            try:
                ls = LoopSpec.model_validate(raw)
            except PlanInvalid:
                raise
            except Exception as e:  # audit-ok[broad_except]: loop.py L274 broad_except structural path not inventing domain value
                raise PlanInvalid(f"invalid LoopSpec: {e}") from e
        else:
            ls = raw
        lid = normalize_loop_id(ls.loop_id)
        if lid != ls.loop_id:
            ls = ls.model_copy(update={"loop_id": lid})
        if lid in seen_ids:
            raise PlanInvalid(f"duplicate loop_id: {lid!r}")
        seen_ids.add(lid)
        max_iter_n = require_exact_int(ls.max_iter, what=f"LoopSpec.max_iter[{lid}]")
        max_loop_cap = require_exact_int(max_loop_iter, what="study.max_loop_iter")
        if max_iter_n < 1:
            raise PlanInvalid(f"LoopSpec.max_iter must be >= 1 (loop {lid!r})")
        if max_iter_n > max_loop_cap:
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
            if any((t.loop_id is not None, t.loop_iter is not None)):
                raise PlanInvalid(
                    f"loop body template {t.task_key!r} must not set loop_id/loop_iter"
                )
            for d in t.depends_on:
                if is_loop_dep(d):
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
        norm_hyps: list[str]
        norm_hyps = []
        for hid in ls.scope_hypotheses:
            if hid is None:
                raise PlanInvalid(
                    f"LoopSpec.scope_hypotheses None id (loop {lid!r})"
                )
            hid_n = unicodedata.normalize("NFKC", str(hid)).strip()
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
        if runtime.study:
            dep_id = task_id(runtime.study.study_id, d)
        else:
            dep_id = d
        dep = optional_item(runtime.tasks, dep_id)
        if dep is None:
            matches = [t for t in runtime.tasks.values() if t.spec.task_key == d]
            dep = matches[0] if matches else None
        if any((dep is None, dep.state != TaskState.DONE)):
            return False
    return True

def _iter_all_done(runtime: StudyRuntime, loop_id: str, iter_n: int) -> bool:
    keys = iter_keys(runtime, loop_id, iter_n)
    if not keys:
        return False
    for k in keys:
        matches = [t for t in runtime.tasks.values() if t.spec.task_key == k]
        if any((not matches, matches[0].state != TaskState.DONE)):
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
    out: list[TaskSpec]
    out = []
    for tmpl in body:
        new_key = key_map[tmpl.task_key]
        deps: list[str]
        deps = []
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
    if runtime.study is None:
        raise JournalFormatError("materialize_loop_iter: missing study")
    if runtime._finalized:
        return None
    study = runtime.study
    sid = study.study_id
    st = _loop_state(runtime)
    raw_row = optional_item(st, loop.loop_id)
    if raw_row is None:
        row: dict[str, Any]
        row = {}
    elif not isinstance(raw_row, dict):
        raise JournalFormatError(
            f"loop state for {loop.loop_id!r} must be dict, got {type(raw_row).__name__}"
        )
    else:
        row = raw_row
    if optional_item(row, "status") in (LoopStatus.DONE.value, LoopStatus.CAPPED.value):
        return None
    # Idempotent: already materialized this iter.
    if "iters" in row:
        iters_map = row["iters"]
        if not isinstance(iters_map, dict):
            raise JournalFormatError(
                f"loop.iters must be dict, got {type(iters_map).__name__}"
            )
        existing = optional_item(iters_map, str(iter_n))
    else:
        existing = None
    if existing:
        return None

    now = runtime.clock.now()
    if iter_n > 1:
        prev_keys = iter_keys(runtime, loop.loop_id, iter_n - 1)
    else:
        prev_keys = []
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
    max_tasks = require_exact_int(study.max_tasks, what="study.max_tasks")
    existing_n = len(runtime.tasks)
    if existing_n + len(added) > max_tasks:
        await _refuse_loop(
            runtime,
            loop,
            detail=f"loop_max_tasks:{existing_n + len(added)}>{max_tasks}",
        )
        return None

    from coscientist.core.plan.expand_validate import task_kind_for_route

    prev = active_plan_blob(runtime)
    if prev is None:
        await _refuse_loop(runtime, loop, detail="missing_plan_rollup")
        return None
    prev_rev = require_exact_int(prev["revision"], what="plan.revision")
    prev_pid = prev["plan_id"]
    if any((not isinstance(prev_pid, str), not prev_pid)):
        await _refuse_loop(runtime, loop, detail="missing_plan_id")
        return None
    new_rev = prev_rev + 1
    prev_tasks = [
        _coerce_model(TaskSpec, t) for t in prev["tasks"]
    ]
    prev_questions = [
        _coerce_model(QuestionSpec, q)
        for q in prev["questions"]
    ]
    if "loops" in prev:
        prev_loops = list(prev["loops"])
    else:
        prev_loops = []
    merged = list(prev_tasks) + added
    share_err = critic_plan_shares(merged, study=study)
    if share_err is not None:
        await _refuse_loop(runtime, loop, detail=f"loop_shares:{share_err}")
        return None

    pid = loop_plan_id(sid, loop.loop_id, iter_n, new_rev)
    if "expand_after" in prev:
        expand_after = list(prev["expand_after"])
    else:
        expand_after = []
    plan = Plan(
        plan_id=pid,
        study_id=sid,
        revision=new_rev,
        hypothesis_id=None,
        tasks=merged,
        questions=list(prev_questions),
        expand_after=expand_after,
        loops=[
            _coerce_model(LoopSpec, x) for x in prev_loops
        ]
        or [loop], rollup="all")  # audit-ok[or_dict]: loop.py L546 or_dict structural path not inventing domain value
    ed = study.effective_deadline()
    events: list[UnstampedEvent]
    events = []
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
    catalog = runtime.catalog
    existing_keys = {t.spec.task_key for t in runtime.tasks.values()}
    for spec in added:
        if spec.task_key in existing_keys:
            continue
        frozen_spec = freeze_task_sandbox_limits(spec, catalog)
        tid = task_id(sid, frozen_spec.task_key)
        try:
            kind = task_kind_for_route(frozen_spec.route)
        except ValueError as e:
            await _refuse_loop(runtime, loop, detail=str(e))
            return None  # audit-ok[except_silent_return]: loop.py L585 except_silent_return structural path not inventing domain value
        from coscientist.core.execution.domain import PlanRole

        task = Task(
            task_id=tid,
            kind=kind,
            spec=frozen_spec,
            state=TaskState.PENDING,
            deadline=ed,
            plan_role=PlanRole.NONE,
        )
        events.append(
            UnstampedEvent(
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
        "loop_max_iter": require_exact_int(loop.max_iter, what="loop.max_iter"),
        "loop_until": loop.until.model_dump(mode="json"),
        "loop_template_keys": list(tmpl_keys),
    }
    events.append(
        UnstampedEvent(
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
    reason: str,
    detail: str | None = None,
    survivors: int | None = None,
) -> None:
    """Journal LoopCapped. Never recomputes survivors on error/refuse paths."""
    if runtime.study is None:
        raise JournalFormatError("_emit_loop_capped: missing study")
    if runtime._finalized:
        return
    sid = runtime.study.study_id
    capped = _require_loop_capped(runtime)
    if loop.loop_id in capped:
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
        "max_iter": require_exact_int(loop.max_iter, what="loop.max_iter"),
        "survivors": survivors,
        "scope_hypotheses": list(loop.scope_hypotheses),
        "scope_questions": list(loop.scope_questions),
        "until": loop.until.model_dump(mode="json"),
        "reason": reason,
    }
    if detail:
        payload["detail"] = detail[:DETAIL_MAX_LEN]
    ev = UnstampedEvent(
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
    reason: str,
) -> None:
    """Journal LoopRefused + LoopCapped + ExpansionRefused in one append batch.

    Single batch avoids mid-emit ``status=refused`` early-outs that would skip
    LoopCapped (live ≡ replay needs ``_loop_capped`` populated).
    """
    if runtime.study is None:
        raise JournalFormatError("_emit_loop_refused: missing study")
    if runtime._finalized:
        return
    if loop_is_complete(runtime, loop.loop_id):
        return
    capped = _require_loop_capped(runtime)
    if loop.loop_id in capped:
        return
    sid = runtime.study.study_id
    now = runtime.clock.now()
    detail_s = detail[:DETAIL_MAX_LEN]
    if loop.depends_on:
        after = loop.depends_on[0]
    else:
        after = loop.loop_id
    until = loop.until.model_dump(mode="json")
    scope_h = list(loop.scope_hypotheses)
    scope_q = list(loop.scope_questions)
    max_i = require_exact_int(loop.max_iter, what="loop.max_iter")
    events = [
        UnstampedEvent(
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
        UnstampedEvent(
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
            },
            ts=now,
            schema_version=LOOP_CAPPED_SCHEMA_VERSION,
),
        UnstampedEvent(
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
    if runtime.study is None:
        raise JournalFormatError("_emit_loop_completed: missing study")
    if runtime._finalized:
        return
    done_map = runtime._loop_done
    if done_map is None:
        raise JournalFormatError("_loop_done missing")
    if not isinstance(done_map, set):
        raise JournalFormatError(
            f"_loop_done must be set, got {type(done_map).__name__}"
        )
    if loop.loop_id in done_map:
        return
    if loop_status(runtime, loop.loop_id) == LoopStatus.DONE.value:
        return
    sid = runtime.study.study_id
    survivors = count_survivors(runtime, loop, upto_iter=iter_n)
    now = runtime.clock.now()
    payload = {
        "loop_id": loop.loop_id,
        "iter": iter_n,
        "max_iter": require_exact_int(loop.max_iter, what="loop.max_iter"),
        "survivors": survivors,
        "scope_hypotheses": list(loop.scope_hypotheses),
        "scope_questions": list(loop.scope_questions),
        "until": loop.until.model_dump(mode="json"),
        "reason": "until_met",
    }
    ev = UnstampedEvent(
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
    reason: str,
) -> None:
    """Journal LoopRefused + LoopCapped(reason≠iteration_cap) for live ≡ replay."""
    cur = current_iter(runtime, loop.loop_id)
    await _emit_loop_refused(
        runtime, loop, iter_n=cur, detail=detail, reason=reason
    )

async def _emit_loop_advance_failed(
    runtime: StudyRuntime, *, detail: str
) -> None:
    """Journal ExpansionRefused when no LoopSpec context is available."""
    if runtime.study is None:
        raise JournalFormatError("_emit_loop_advance_failed: missing study")
    if runtime._finalized:
        return
    sid = runtime.study.study_id
    now = runtime.clock.now()
    detail_s = detail[:DETAIL_MAX_LEN]
    await runtime._append(  # noqa: SLF001
        [
            UnstampedEvent(
                study_id=sid,
                type="ExpansionRefused",
                entity_key=f"{sid}:loop_advance:error",
                idem_key=make_idem("ExpansionRefused", sid, "loop_advance", detail_s),
                payload={
                    "after_task": "loop_advance",
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
    if any((runtime.study is None, runtime._finalized)):
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
                    iter_n=current_iter(runtime, loop.loop_id),
                    detail=detail,
                    reason=LOOP_REASON_ERROR,
                )
            else:
                await _emit_loop_advance_failed(runtime, detail=detail)
        except LeaseLost:
            raise
        except Exception as journal_exc:  # noqa: BLE001  # audit-ok[broad_except]: loop.py L908 broad_except structural path not inventing domain value
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
        except Exception as close_exc:  # noqa: BLE001  # audit-ok[broad_except]: loop.py L925 broad_except structural path not inventing domain value
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
    if any((runtime.study is None, runtime._finalized)):
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
                raise JournalFormatError(
                    f"parse_loop_dep returned None after is_loop_dep for {d!r}"
                )
            if loop_is_complete(runtime, lid):
                continue
            if loop_known(runtime, lid):
                continue
            # Unknown loop id — refuse the waiter (honest, not a hang).
            await runtime.cancel_task(
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

    if cur >= require_exact_int(loop.max_iter, what="loop.max_iter"):
        await _emit_loop_capped(
            runtime, loop, iter_n=cur, reason=LOOP_REASON_ITERATION_CAP
        )
        return None

    return await materialize_loop_iter(runtime, loop, iter_n=cur + 1)

async def maybe_advance_loops(runtime: StudyRuntime) -> list[str]:
    """Advance all loops that can take a step (start / next iter / cap / complete).

    Expected eval failures (unknown predicate) → LoopRefused + LoopCapped(reason=error)
    without finalizing the study. Any other exception → journal + finalize
    ``internal_error`` (never leave TaskDone without StudyFinalized; never
    disguise as iteration_cap). Does not re-raise after finalize.
    """
    done: list[str]
    done = []
    if any((runtime.study is None, runtime._finalized)):
        return done
    try:
        await refuse_orphaned_loop_barriers(runtime)
        loops = _loops_from_plans(runtime)
    except LeaseLost:
        raise
    except Exception as e:  # noqa: BLE001  # audit-ok[broad_except]: loop.py L1018 broad_except structural path not inventing domain value
        await finalize_loop_internal_error(runtime, exc=e, loop=None)
        return done  # audit-ok[except_silent_return]: loop.py L1020 except_silent_return structural path not inventing domain value
    for loop in loops:
        if runtime._finalized:
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
            except Exception as refuse_exc:  # noqa: BLE001  # audit-ok[broad_except]: loop.py L1038 broad_except structural path not inventing domain value
                await finalize_loop_internal_error(
                    runtime, exc=refuse_exc, loop=loop
                )
                break
        except LeaseLost:
            raise
        except Exception as e:  # noqa: BLE001  # audit-ok[broad_except]: loop.py L1045 broad_except structural path not inventing domain value
            await finalize_loop_internal_error(runtime, exc=e, loop=loop)
            break
    return done

async def maybe_loop_after_task_done(
    runtime: StudyRuntime, task_id_full: str
) -> list[str]:
    """After any TaskDone, try to advance loops (deps ready or iter complete)."""
    if any((runtime.study is None, runtime._finalized)):
        return []
    return await maybe_advance_loops(runtime)

async def resume_pending_loops(runtime: StudyRuntime) -> int:
    """Recover: re-advance loops so live == replay.

    Unexpected errors are journaled + finalized inside ``maybe_advance_loops``;
    this wrapper also catch-finalizes so recover never aborts before ``tick()``.
    """
    if any((runtime.study is None, runtime._finalized)):
        return 0
    try:
        got = await maybe_advance_loops(runtime)
        return len(got)
    except LeaseLost:
        raise
    except Exception as e:  # noqa: BLE001  # audit-ok[broad_except]: loop.py L1071 broad_except structural path not inventing domain value
        await finalize_loop_internal_error(runtime, exc=e, loop=None)
        return 0  # audit-ok[except_silent_return]: loop.py L1073 except_silent_return structural path not inventing domain value

def fold_loop_plan_expanded(runtime: Any, payload: dict[str, Any]) -> None:
    """Rebuild ``_loop_state`` from a PlanExpanded(loop=true) payload."""
    if not optional_item(payload, "loop"):
        return
    if any(('loop_id' not in payload, not isinstance(payload['loop_id'], str), not payload['loop_id'])):
        raise JournalFormatError("PlanExpanded(loop): missing loop_id")
    lid = payload["loop_id"]
    try:
        iter_n = require_exact_int(payload["loop_iter"], what="PlanExpanded.loop_iter")
    except KeyError as e:
        raise JournalFormatError("PlanExpanded(loop): missing 'loop_iter'") from e
    if "added_tasks" not in payload:
        raise JournalFormatError("PlanExpanded(loop): missing added_tasks")
    raw_added = payload["added_tasks"]
    if not isinstance(raw_added, list):
        raise JournalFormatError("PlanExpanded(loop): added_tasks must be list")
    keys: list[str]
    keys = []
    for t in raw_added:
        if isinstance(t, dict):
            if any(('task_key' not in t, not isinstance(t['task_key'], str), not t['task_key'])):
                raise JournalFormatError("PlanExpanded(loop): added_tasks entry missing task_key")
            keys.append(t["task_key"])
        elif isinstance(t, TaskSpec):
            tk = t.task_key
            if any((not isinstance(tk, str), not tk)):
                raise JournalFormatError("PlanExpanded(loop): added_tasks entry missing task_key")
            keys.append(tk)
        else:
            raise JournalFormatError(
                f"PlanExpanded(loop): added_tasks entry must be dict|TaskSpec, got {type(t).__name__}"
            )
    st = _loop_state(runtime)
    if lid in st:
        prev = st[lid]
        if not isinstance(prev, dict):
            raise JournalFormatError(
                f"PlanExpanded(loop): prior state must be dict, got {type(prev).__name__}"
            )
        row = dict(prev)
    else:
        row = {}
    if "iters" in row:
        raw_iters = row["iters"]
        if not isinstance(raw_iters, dict):
            raise JournalFormatError("PlanExpanded(loop): iters must be dict")
        iters = dict(raw_iters)
    else:
        iters = {}
    iters[str(iter_n)] = keys
    row["iters"] = iters
    # Missing iter = first PlanExpanded for this loop (no prior tip).
    # When present, require_exact_int rejects bool/str/float (False must not bypass).
    if "iter" in row:
        prev_iter = require_exact_int(row["iter"], what="loop.iter")
        row["iter"] = max(prev_iter, iter_n)
    else:
        row["iter"] = iter_n
    if optional_item(row, "status") not in (LoopStatus.DONE.value, LoopStatus.CAPPED.value):
        row["status"] = LoopStatus.RUNNING.value
    st[lid] = row

def fold_loop_capped(runtime: Any, payload: dict[str, Any]) -> None:
    if any(('loop_id' not in payload, not isinstance(payload['loop_id'], str), not payload['loop_id'])):
        raise JournalFormatError("LoopCapped: missing loop_id")
    lid = payload["loop_id"]
    reason = _require_loop_reason(
        payload, key="reason", allowed=LOOP_CAPPED_REASONS, what="LoopCapped"
    )
    iter_n = _require_loop_iter(payload, what="LoopCapped")
    st = _loop_state(runtime)
    if lid in st:
        prev = st[lid]
        if not isinstance(prev, dict):
            raise JournalFormatError(
                f"LoopCapped: prior state must be dict, got {type(prev).__name__}"
            )
        row = dict(prev)
    else:
        row = {}
    if reason in (LOOP_REASON_ERROR, LOOP_REASON_REFUSED):
        row["status"] = LoopStatus.REFUSED.value
    else:
        row["status"] = LoopStatus.CAPPED.value
    row["iter"] = iter_n
    if "survivors" in payload:
        row["survivors"] = payload["survivors"]
    row["reason"] = reason
    if "detail" in payload and payload["detail"] is not None:
        detail_v = payload["detail"]
        if not isinstance(detail_v, str):
            raise JournalFormatError(
                f"LoopCapped.detail must be str, got {type(detail_v).__name__}"
            )
        row["detail"] = detail_v[:DETAIL_MAX_LEN]
    st[lid] = row
    capped = _require_loop_capped(runtime)
    capped[lid] = dict(payload)

def fold_loop_refused(runtime: Any, payload: dict[str, Any]) -> None:
    """Rebuild refused status from LoopRefused (before LoopCapped on the same path)."""
    if any(('loop_id' not in payload, not isinstance(payload['loop_id'], str), not payload['loop_id'])):
        raise JournalFormatError("LoopRefused: missing loop_id")
    lid = payload["loop_id"]
    st = _loop_state(runtime)
    if lid in st:
        prev = st[lid]
        if not isinstance(prev, dict):
            raise JournalFormatError(
                f"LoopRefused: prior state must be dict, got {type(prev).__name__}"
            )
        row = dict(prev)
    else:
        row = {}
    if optional_item(row, "status") in ("done", "capped", "refused"):
        return
    reason = _require_loop_reason(
        payload, key="reason", allowed=LOOP_REFUSED_REASONS, what="LoopRefused"
    )
    iter_n = _require_loop_iter(payload, what="LoopRefused")
    row["status"] = LoopStatus.REFUSED.value
    row["reason"] = reason
    row["iter"] = iter_n
    if "detail" in payload and payload["detail"] is not None:
        detail_v = payload["detail"]
        if not isinstance(detail_v, str):
            raise JournalFormatError(
                f"LoopRefused.detail must be str, got {type(detail_v).__name__}"
            )
        row["detail"] = detail_v[:DETAIL_MAX_LEN]
    st[lid] = row

def fold_loop_completed(runtime: Any, payload: dict[str, Any]) -> None:
    """Rebuild until-met completion from journal (live ≡ replay)."""
    if any(('loop_id' not in payload, not isinstance(payload['loop_id'], str), not payload['loop_id'])):
        raise JournalFormatError("LoopCompleted: missing loop_id")
    lid = payload["loop_id"]
    reason = _require_loop_reason(
        payload, key="reason", allowed=LOOP_COMPLETED_REASONS, what="LoopCompleted"
    )
    iter_n = _require_loop_iter(payload, what="LoopCompleted")
    st = _loop_state(runtime)
    if lid in st:
        prev = st[lid]
        if not isinstance(prev, dict):
            raise JournalFormatError(
                f"LoopCompleted: prior state must be dict, got {type(prev).__name__}"
            )
        row = dict(prev)
    else:
        row = {}
    row["status"] = LoopStatus.DONE.value
    row["iter"] = iter_n
    if "survivors" in payload:
        row["survivors"] = payload["survivors"]
    row["complete_reason"] = reason
    st[lid] = row
    done = runtime._loop_done
    if done is None:
        raise JournalFormatError("_loop_done missing")
    if not isinstance(done, set):
        raise JournalFormatError(
            f"_loop_done must be set, got {type(done).__name__}"
        )
    done.add(lid)

def fold_loop_expansion_refused(runtime: Any, payload: dict[str, Any]) -> None:
    """ExpansionRefused for a loop restores terminal status (barrier / no re-advance).

    Does not populate ``_loop_capped`` (LoopCapped's job).
    """
    if not optional_item(payload, "loop") and "loop_id" not in payload:
        return
    if any(('loop_id' not in payload, not isinstance(payload['loop_id'], str), not payload['loop_id'])):
        raise JournalFormatError("ExpansionRefused(loop): missing loop_id")
    lid = payload["loop_id"]
    st = _loop_state(runtime)
    if lid in st:
        prev = st[lid]
        if not isinstance(prev, dict):
            raise JournalFormatError(
                f"ExpansionRefused(loop): prior state must be dict, got {type(prev).__name__}"
            )
        row = dict(prev)
    else:
        row = {}
    if optional_item(row, "status") in ("done", "capped", "refused"):
        return
    loop_reason = _require_loop_reason(
        payload,
        key="loop_reason",
        allowed=LOOP_CAPPED_REASONS,
        what="ExpansionRefused(loop)",
    )
    if loop_reason in (LOOP_REASON_ERROR, LOOP_REASON_REFUSED):
        row["status"] = LoopStatus.REFUSED.value
    else:
        row["status"] = LoopStatus.CAPPED.value
    row["reason"] = loop_reason
    if "detail" in payload and payload["detail"] is not None:
        detail_v = payload["detail"]
        if not isinstance(detail_v, str):
            raise JournalFormatError(
                f"ExpansionRefused.detail must be str, got {type(detail_v).__name__}"
            )
        row["detail"] = detail_v[:DETAIL_MAX_LEN]
    st[lid] = row

def capped_scope_questions(runtime: Any) -> dict[str, dict[str, Any]]:
    """qid → LoopCapped payload only for true iteration_cap with ≥1 iter.

    Refuse/error terminals and zero-iter caps are excluded — they must not stamp
    disposition reason=iteration_cap. Post-#21 rebase: also respect public
    ``TerminalReason.INTERNAL_ERROR`` (never map error-path loops to cap).
    """
    capped = _require_loop_capped(runtime)
    out: dict[str, dict[str, Any]]
    out = {}
    for _lid, p in capped.items():
        if not isinstance(p, dict):
            raise JournalFormatError(
                f"LoopCapped payload must be dict, got {type(p).__name__}"
            )
        reason = _require_loop_reason(
            p, key="reason", allowed=LOOP_CAPPED_REASONS, what="LoopCapped"
        )
        if reason != LOOP_REASON_ITERATION_CAP:
            continue
        iter_n = _require_loop_iter(p, what="LoopCapped")
        if iter_n < 1:
            continue
        if "scope_questions" not in p:
            raise JournalFormatError("LoopCapped: missing scope_questions")
        scope_q = p["scope_questions"]
        if not isinstance(scope_q, list):
            raise JournalFormatError("LoopCapped: scope_questions must be list")
        for qid in scope_q:
            out[str(qid)] = p
    return out
