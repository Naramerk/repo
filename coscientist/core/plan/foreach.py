"""Foreach materialization (M1 PR10): PlanExpanded(foreach=true), no planner/quota."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from coscientist.core.claims.reasons import TaskReason
from coscientist.core.events.errors import JournalFormatError, LeaseLost
from coscientist.core.events.events import (
    UnstampedEvent,
    make_idem,
)
from coscientist.core.events.ids import task_id
from coscientist.core.events.schema_versions import (
    PLAN_EXPANDED_SCHEMA_VERSION,
    TASK_CREATED_SCHEMA_VERSION,
)
from coscientist.core.events.upcasters import require_exact_int
from coscientist.core.execution.domain import (
    ForEachSpec,
    InputSpec,
    Plan,
    QuestionSpec,
    Task,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.plan.expand_graph import (
    plan_graph_events,
    question_graph_events,
    skip_superseded_task_events,
)
from coscientist.core.plan.expand_trigger import refuse_expansion
from coscientist.core.plan.expand_validate import active_plan_blob, assign_core_keys

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime

_NAT_SPLIT = re.compile(r"(\d+)")

def _nat_key(s: str) -> tuple:
    """Numerically aware sort key: H2 < H10."""
    if not isinstance(s, str):
        raise TypeError(f"_nat_key requires str, got {type(s).__name__}")
    parts = _NAT_SPLIT.split(s)
    out: list[Any] = []
    for p in parts:
        if p.isdigit():
            out.append(int(p))
        else:
            out.append(p.casefold())
    return tuple(out)

def foreach_plan_id(study_id: str, template_key: str, rev: int) -> str:
    """Deterministic plan id (no random) so idem/replay match live."""
    return f"pl_foreach_{study_id}_{template_key}_r{rev}"

def foreach_templates(runtime: StudyRuntime) -> list[Task]:
    """PENDING tasks whose spec.foreach is set."""
    out: list[Task] = []
    for t in runtime.tasks.values():
        if t.state == TaskState.DONE:
            continue
        if t.spec.foreach is not None:
            out.append(t)
    return out

def _rank_key(logical: str, attrs: dict[str, Any]) -> tuple[tuple, str | None]:
    """Sort key: numeric rank first; missing/non-numeric rank sorts LAST.

    Never raises — non-numeric yields a report note.
    """
    raw = attrs.get("rank", None)
    if raw is None:
        return (1, 0, _nat_key(logical)), None
    if isinstance(raw, bool) or not isinstance(raw, int):
        # Non-exact-int rank → note + sort last (never invent a numeric rank).
        note = f"foreach: нечисловой rank у {logical} ({raw!r})"
        return (1, 0, _nat_key(logical)), note
    r = raw
    return (0, r, _nat_key(logical)), None

def select_foreach_items(
    runtime: StudyRuntime,
    fe: ForEachSpec,
) -> tuple[list[str], list[str]]:
    """Logical ids + rank notes; ``source_task == of_task`` required.

    Cap keeps the head of rank order (missing/non-numeric last). Notes list
    non-numeric ranks for the report (template is never SKIPPED for bad rank).
    """
    try:
        graph = runtime.graph
    except AttributeError as e:
        raise JournalFormatError("foreach requires runtime.graph") from e
    if graph is None:
        raise JournalFormatError("foreach requires runtime.graph")
    try:
        nodes = list(graph.nodes_by_kind(fe.kind))
    except KeyError as e:
        raise JournalFormatError(f"foreach unknown graph kind: {fe.kind!r}") from e
    scored: list[tuple[tuple, str]] = []
    notes: list[str] = []
    for node in nodes:
        if node.attrs is None:
            attrs = {}
        else:
            attrs = dict(node.attrs)
        src = attrs.get("source_task")
        if src is None or str(src) != fe.of_task:
            continue
        if isinstance(node.ref_id, str) and node.ref_id:
            logical = node.ref_id
        elif isinstance(attrs.get("id"), str) and attrs["id"]:
            logical = attrs["id"]
        else:
            raise JournalFormatError(
                f"foreach node {node.node_id!r} missing structural id/ref_id"
            )
        if fe.selector == "selected":
            if "selected" not in attrs:
                continue  # absent selected ≡ not selected (no invent True)
            sel = attrs["selected"]
            if not isinstance(sel, bool):
                raise JournalFormatError(
                    f"foreach node selected must be bool, got {type(sel).__name__}"
                )
            if not sel:
                continue
        key, note = _rank_key(logical, attrs)
        if note:
            notes.append(note)
        scored.append((key, logical))
    scored.sort(key=lambda x: x[0])
    return [s[1] for s in scored], notes

def _concrete_from_template(
    template: TaskSpec,
    *,
    item_id: str,
    task_key: str,
) -> TaskSpec:
    fe = template.foreach
    if fe is None:
        raise JournalFormatError("foreach template missing ForEachSpec")
    addresses = list(template.addresses)
    if fe.kind == "question" and item_id not in addresses:
        addresses = [*addresses, item_id]
    return template.model_copy(
        update={
            "task_key": task_key,
            "title": f"{template.title}:{item_id}",
            "foreach": None,
            "foreach_item": item_id,
            "addresses": addresses,
        }
    )

def expand_input_groups(
    runtime: Any, specs: list[InputSpec]
) -> list[tuple[InputSpec, list[InputSpec]]]:
    """Per-original-InputSpec groups after foreach child expansion.

    ``from_task`` naming a foreach template → one member per child (may be []).
    Other inputs → single-member group. Quorum stays on the **original** spec.
    """
    try:
        children = runtime._foreach_children
    except AttributeError:
        children = {}
    if children is None:
        children = {}
    if not isinstance(children, dict):
        raise JournalFormatError(
            f"_foreach_children must be dict, got {type(children).__name__}"
        )
    groups: list[tuple[InputSpec, list[InputSpec]]] = []
    for inp in specs:
        if inp.from_task in children:
            kids = list(children[inp.from_task])
            members = [inp.model_copy(update={"from_task": k, "quorum": None}) for k in kids]
            groups.append((inp, members))
        else:
            groups.append((inp, [inp]))
    return groups

def foreach_already_done(runtime: StudyRuntime, template_key: str) -> bool:
    """True when a foreach PlanExpanded already superseded this template."""
    for p in runtime.plans.values():
        if not p.get("foreach"):
            continue
        raw_tmpl = p.get("foreach_template")
        if isinstance(raw_tmpl, str) and raw_tmpl == template_key:
            return True
        if "superseded_tasks" in p:
            superseded = p["superseded_tasks"]
            if superseded is None:
                raise JournalFormatError("plan superseded_tasks is None")
        else:
            superseded = []
        if template_key in superseded:
            return True
    return False

def _source_ready(runtime: StudyRuntime, of_task: str) -> Task | None:
    """Return DONE source task if present (any outcome); else None."""
    for t in runtime.tasks.values():
        if t.spec.task_key != of_task:
            continue
        if t.state == TaskState.DONE:
            return t
    return None

def _items_for_source(
    runtime: StudyRuntime, src: Task, fe: ForEachSpec
) -> tuple[list[str], list[str]]:
    if src.outcome in (TaskOutcome.SUCCESS, TaskOutcome.PARTIAL):
        return select_foreach_items(runtime, fe)
    return [], []

async def _refuse_and_skip_foreach(
    runtime: StudyRuntime,
    template: Task,
    *,
    after: str,
    detail: str,
) -> None:
    """ExpansionRefused + TaskDone SKIPPED so the template is never dispatched."""
    await refuse_expansion(
        runtime, after, reason="invalid", detail=detail[:200]
    )
    if template.state != TaskState.DONE:
        await runtime.cancel_task(  # type: ignore[attr-defined]
            template.task_id,
            reason=TaskReason.REFUSED,
            detail=detail[:200],
        )

async def materialize_foreach(
    runtime: StudyRuntime,
    *,
    template: Task,
    items: list[str],
    rank_notes: list[str] | None = None,
) -> str | None:
    """Write PlanExpanded(foreach=true). Returns plan_id, or None if refused/closed."""
    if runtime.study is None:
        return None
    try:
        finalized = runtime._finalized
    except AttributeError as e:
        raise JournalFormatError("materialize_foreach: runtime missing _finalized") from e
    if finalized:
        return None
    study = runtime.study
    sid = study.study_id
    now = runtime.clock.now()
    tkey = template.spec.task_key
    if foreach_already_done(runtime, tkey):
        return None

    fe = template.spec.foreach
    if fe is None:
        raise JournalFormatError(f"foreach template {tkey!r} missing ForEachSpec")
    after = fe.of_task
    cap = require_exact_int(study.max_foreach_children, what="study.max_foreach_children")
    # ``items`` arrive rank-ordered; cap keeps head (missing rank already last).
    foreach_total = len(items)
    dropped: list[str] = []
    capped = False
    if foreach_total > cap:
        dropped = list(items[cap:])
        items = list(items[:cap])
        capped = True
    items = sorted(items, key=_nat_key)  # H2 < H10 among kept
    if rank_notes is None:
        notes: list[str] = []
    else:
        notes = list(rank_notes)

    prototypes = [
        _concrete_from_template(template.spec, item_id=item, task_key=f"TMP-{i}")
        for i, item in enumerate(items)
    ]
    if prototypes:
        keyed = assign_core_keys(runtime, prototypes)
        if isinstance(keyed, str):
            await _refuse_and_skip_foreach(
                runtime,
                template,
                after=after,
                detail=f"foreach_assign_keys:{keyed}",
            )
            return None
        from coscientist.core.plan.shares import apply_foreach_parent_shares

        prev0 = active_plan_blob(runtime)
        if prev0 is None:
            prev0 = {}
        if "tasks" in prev0:
            raw_tasks = prev0["tasks"]
            if raw_tasks is None:
                raise JournalFormatError("plan blob tasks is None")
        else:
            raw_tasks = []
        plan_specs = [
            TaskSpec.model_validate(t) if isinstance(t, dict) else t
            for t in raw_tasks
        ]
        reserve = study.replan_reserve
        if type(reserve) is bool or not isinstance(reserve, (int, float)):
            raise JournalFormatError(
                f"study.replan_reserve must be numeric, got {type(reserve).__name__}"
            )
        share_cap = 1.0 - float(reserve)
        added = apply_foreach_parent_shares(
            template.spec, list(keyed), plan_tasks=plan_specs, share_cap=share_cap
        )
    else:
        added = []
        prev0 = active_plan_blob(runtime)
        if prev0 is None:
            prev0 = {}
        plan_specs = []
    from coscientist.core.plan.shares import critic_plan_shares

    prev = prev0
    if "revision" in prev:
        prev_rev = require_exact_int(prev["revision"], what="plan.revision")
    else:
        prev_rev = 0
    if "plan_id" in prev:
        raw_pid = prev["plan_id"]
        if not isinstance(raw_pid, str):
            raise JournalFormatError(
                f"plan.plan_id must be str, got {type(raw_pid).__name__}"
            )
        prev_pid = raw_pid
    else:
        prev_pid = ""
    new_rev = prev_rev + 1
    if plan_specs:
        prev_tasks = plan_specs
    elif "tasks" in prev:
        raw_pt = prev["tasks"]
        if raw_pt is None:
            raise JournalFormatError("plan blob tasks is None")
        prev_tasks = [
            TaskSpec.model_validate(t) if isinstance(t, dict) else t for t in raw_pt
        ]
    else:
        prev_tasks = []
    if "questions" in prev:
        raw_pq = prev["questions"]
        if raw_pq is None:
            raise JournalFormatError("plan blob questions is None")
        prev_questions = [
            QuestionSpec.model_validate(q) if isinstance(q, dict) else q
            for q in raw_pq
        ]
    else:
        prev_questions = []
    new_keys, superseded = [t.task_key for t in added], [tkey]
    merged = [t for t in prev_tasks if t.task_key != tkey] + added
    share_err = critic_plan_shares(merged, study=study)
    if share_err is not None:
        await _refuse_and_skip_foreach(
            runtime, template, after=after, detail=f"foreach_shares:{share_err}"
        )
        return None
    pid = foreach_plan_id(sid, tkey, new_rev)
    from coscientist.core.execution.domain import LoopSpec

    if "loops" in prev:
        raw_loops = prev["loops"]
        if raw_loops is None:
            raise JournalFormatError("plan blob loops is None")
        prev_loops = [
            LoopSpec.model_validate(x) if isinstance(x, dict) else x
            for x in raw_loops
        ]
    else:
        prev_loops = []
    if "expand_after" in prev and prev["expand_after"] is not None:
        prev_expand_after = list(prev["expand_after"])
    else:
        prev_expand_after = []
    plan = Plan(
        plan_id=pid,
        study_id=sid,
        revision=new_rev,
        hypothesis_id=None,
        tasks=merged,
        questions=list(prev_questions),
        expand_after=prev_expand_after,
        loops=prev_loops, rollup="all")
    ed = study.effective_deadline()
    events: list[UnstampedEvent] = []
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
    events.extend(
        skip_superseded_task_events(
            runtime, sid=sid, superseded_tasks=superseded, now=now
        )
    )
    from coscientist.checks.sandbox_limits import freeze_task_sandbox_limits
    from coscientist.core.plan.expand_validate import task_kind_for_route

    catalog = runtime.catalog
    existing_keys = {t.spec.task_key for t in runtime.tasks.values()}
    for spec in added:
        if spec.task_key in existing_keys and spec.task_key not in superseded:
            continue
        frozen_spec = freeze_task_sandbox_limits(spec, catalog)
        tid = task_id(sid, frozen_spec.task_key)
        kind = task_kind_for_route(frozen_spec.route)
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
        "after_task": after,
        "plan_rev": new_rev,
        "added_tasks": [t.model_dump(mode="json") for t in added],
        "added_questions": [],
        "superseded_tasks": superseded,
        "superseded_questions": [],
        "origin": "plan",
        "reason": f"foreach:{tkey}",
        "delegated_by": None,
        "replan_allocated_s": 0.0,
        "prev_plan_id": prev_pid or None,
        "hitl_request_id": None,
        "plan_key": None,
        "foreach": True,
        "foreach_template": tkey,
        "foreach_items": list(items),
        "foreach_kind": fe.kind if fe else None,
        "foreach_capped": capped,
        "foreach_cap": cap,
        "foreach_total": foreach_total,
        "foreach_dropped": list(dropped),
        "foreach_rank_notes": notes,
        "foreach_refute": (template.spec.tests_intent == "refute"),
    }
    events.append(
        UnstampedEvent(
            study_id=sid,
            type="PlanExpanded",
            entity_key=pid,
            idem_key=make_idem("PlanExpanded", "foreach", sid, tkey),
            payload=payload,
            ts=now,
            schema_version=PLAN_EXPANDED_SCHEMA_VERSION,
                )
    )
    await runtime._append(events)  # noqa: SLF001
    try:
        children = runtime._foreach_children
    except AttributeError:
        runtime._foreach_children = {}  # noqa: SLF001
        children = runtime._foreach_children  # noqa: SLF001
    if children is None:
        runtime._foreach_children = {}  # noqa: SLF001
        children = runtime._foreach_children  # noqa: SLF001
    if not isinstance(children, dict):
        raise JournalFormatError(
            f"_foreach_children must be dict, got {type(children).__name__}"
        )
    children[tkey] = list(new_keys)
    return pid

async def _materialize_one(runtime: StudyRuntime, tmpl: Task) -> str | None:
    fe = tmpl.spec.foreach
    if fe is None:
        return None
    if foreach_already_done(runtime, tmpl.spec.task_key):
        return None
    src = _source_ready(runtime, fe.of_task)
    if src is None:
        return None
    items, notes = _items_for_source(runtime, src, fe)
    return await materialize_foreach(
        runtime, template=tmpl, items=items, rank_notes=notes
    )

async def maybe_foreach_after_task_done(
    runtime: StudyRuntime, task_id_full: str
) -> list[str]:
    """After source TaskDone, materialize any foreach templates waiting on it."""
    done: list[str] = []
    if runtime.study is None:
        return done
    try:
        finalized = runtime._finalized
    except AttributeError as e:
        raise JournalFormatError("maybe_foreach: runtime missing _finalized") from e
    if finalized:
        return done
    src = runtime.tasks.get(task_id_full)
    if src is None or src.state != TaskState.DONE:
        return done
    reason = src.reason
    if reason is None:
        rv = None
    elif isinstance(reason, TaskReason):
        rv = reason.value
    elif isinstance(reason, str):
        rv = reason
    else:
        raise JournalFormatError(
            f"task reason must be TaskReason|str|None, got {type(reason).__name__}"
        )
    if rv == TaskReason.SUPERSEDED.value:
        return done
    after = src.spec.task_key
    for tmpl in list(foreach_templates(runtime)):
        fe = tmpl.spec.foreach
        if fe is None or fe.of_task != after:
            continue
        try:
            pid = await _materialize_one(runtime, tmpl)
            if pid:
                done.append(pid)
        except LeaseLost:
            raise
        except (ValueError, TypeError, JournalFormatError, ValidationError) as e:
            await _refuse_and_skip_foreach(
                runtime,
                tmpl,
                after=after,
                detail=f"foreach_exception:{type(e).__name__}",
            )
    return done

async def materialize_ready_foreach(runtime: StudyRuntime) -> list[str]:
    """Expand every PENDING foreach whose ``of_task`` is already DONE.

    Called from ``accept_plan`` and after ``PlanExpanded`` (B1).
    """
    done: list[str] = []
    if runtime.study is None:
        return done
    try:
        finalized = runtime._finalized
    except AttributeError as e:
        raise JournalFormatError("materialize_ready_foreach: runtime missing _finalized") from e
    if finalized:
        return done
    for tmpl in list(foreach_templates(runtime)):
        fe = tmpl.spec.foreach
        if fe is None:
            after = tmpl.spec.task_key
        else:
            after = fe.of_task
        try:
            pid = await _materialize_one(runtime, tmpl)
            if pid:
                done.append(pid)
        except LeaseLost:
            raise
        except (ValueError, TypeError, JournalFormatError, ValidationError) as e:
            await _refuse_and_skip_foreach(
                runtime,
                tmpl,
                after=after,
                detail=f"foreach_exception:{type(e).__name__}",
            )
    return done

async def resume_pending_foreach(runtime: StudyRuntime) -> int:
    """Re-run foreach after crash / reopen (live == replay)."""
    if runtime.study is None:
        return 0
    try:
        finalized = runtime._finalized
    except AttributeError as e:
        raise JournalFormatError("resume_pending_foreach: runtime missing _finalized") from e
    if finalized:
        return 0
    got = await materialize_ready_foreach(runtime)
    return len(got)

def validate_foreach_specs(tasks: list[TaskSpec], *, known_keys: set[str]) -> None:
    """Reject invalid foreach at accept_plan / validate_expansion."""
    keys = {t.task_key for t in tasks} | known_keys
    from coscientist.core.execution.domain import STAGE_DEP_KEYS as stage
    for t in tasks:
        fe = t.foreach
        if fe is None:
            continue
        if t.foreach_item is not None:
            raise ValueError(
                f"foreach template {t.task_key!r} must not set foreach_item"
            )
        if not fe.of_task:
            raise ValueError(f"foreach.of_task required (task {t.task_key!r})")
        if fe.of_task not in keys and fe.of_task not in stage:
            raise ValueError(
                f"foreach.of_task {fe.of_task!r} unknown (task {t.task_key!r})"
            )
        if fe.of_task not in t.depends_on:
            raise ValueError(
                f"foreach template {t.task_key!r} must depend_on {fe.of_task!r}"
            )
