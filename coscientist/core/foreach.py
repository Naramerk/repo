"""Foreach materialization (M1 PR10 / S9).

Expands ``TaskSpec.foreach`` into N concrete tasks without planner or expansion
quota. Journaled as ``PlanExpanded`` with ``foreach=true`` (replay == live).

Fires on source ``TaskDone``, at ``accept_plan`` when ``of_task`` is already
DONE, and after ``PlanExpanded`` when newly added templates have a DONE source.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from coscientist.core.domain import (
    ForEachSpec,
    InputSpec,
    Plan,
    QuestionSpec,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.expand_graph import (
    plan_graph_events,
    question_graph_events,
    skip_superseded_task_events,
)
from coscientist.core.expand_trigger import refuse_expansion
from coscientist.core.expand_validate import active_plan_blob, assign_core_keys
from coscientist.core.ids import task_id
from coscientist.core.reasons import TaskReason

if TYPE_CHECKING:
    from coscientist.core.machine import StudyRuntime

_NAT_SPLIT = re.compile(r"(\d+)")


def _nat_key(s: str) -> tuple:
    """Numerically aware sort key: H2 < H10."""
    parts = _NAT_SPLIT.split(str(s))
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


def select_foreach_items(
    runtime: StudyRuntime,
    fe: ForEachSpec,
) -> list[str]:
    """Logical ids with ``source_task == of_task`` matching the selector.

    Nodes without ``source_task`` are rejected. Order is rank ascending, then
    numerically aware id (H2 < H10) — cap keeps the head of this order.
    """
    graph = getattr(runtime, "graph", None)
    if graph is None:
        return []
    try:
        nodes = list(graph.nodes_by_kind(fe.kind))
    except KeyError:
        return []
    items: list[tuple[str, dict[str, Any]]] = []
    for node in nodes:
        attrs = dict(node.attrs or {})
        src = attrs.get("source_task")
        if src is None or str(src) != fe.of_task:
            continue
        logical = str(node.ref_id or attrs.get("id") or "")
        if not logical:
            nid = str(node.node_id)
            logical = nid.rsplit(":", 1)[-1] if ":" in nid else nid
        if not logical:
            continue
        if fe.selector == "selected" and not bool(attrs.get("selected", False)):
            continue
        items.append((logical, attrs))
    items.sort(key=lambda x: (int(x[1].get("rank") or 0), _nat_key(x[0])))
    return [i[0] for i in items]


def _concrete_from_template(
    template: TaskSpec,
    *,
    item_id: str,
    task_key: str,
) -> TaskSpec:
    fe = template.foreach
    assert fe is not None
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
    children = getattr(runtime, "_foreach_children", None) or {}
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
        if str(p.get("foreach_template") or "") == template_key:
            return True
        superseded = p.get("superseded_tasks") or []
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


def _items_for_source(runtime: StudyRuntime, src: Task, fe: ForEachSpec) -> list[str]:
    if src.outcome in (TaskOutcome.SUCCESS, TaskOutcome.PARTIAL):
        return select_foreach_items(runtime, fe)
    return []


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
) -> str | None:
    """Write PlanExpanded(foreach=true). Returns plan_id, or None if refused/closed."""
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return None
    study = runtime.study
    sid = study.study_id
    now = runtime.clock.now()
    tkey = template.spec.task_key
    if foreach_already_done(runtime, tkey):
        return None

    fe = template.spec.foreach
    after = fe.of_task if fe else tkey
    cap = int(getattr(study, "max_foreach_children", 32) or 32)
    # ``items`` arrive rank-ordered; cap keeps head, drop natural-sort would be wrong.
    foreach_total = len(items)
    dropped: list[str] = []
    capped = False
    if foreach_total > cap:
        dropped = list(items[cap:])
        items = list(items[:cap])
        capped = True
    # Child dispatch order: numerically aware (H2 < H10).
    items = sorted(items, key=_nat_key)

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
        added: list[TaskSpec] = list(keyed)
    else:
        added = []

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

    new_keys = [t.task_key for t in added]
    superseded = [tkey]
    still_kept = [t for t in prev_tasks if t.task_key != tkey]
    merged = still_kept + added
    pid = foreach_plan_id(sid, tkey, new_rev)
    plan = Plan(
        plan_id=pid,
        study_id=sid,
        revision=new_rev,
        hypothesis_id=None,
        tasks=merged,
        questions=list(prev_questions),
        expand_after=list(prev.get("expand_after") or []),
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
    events.extend(
        skip_superseded_task_events(
            runtime, sid=sid, superseded_tasks=superseded, now=now
        )
    )
    existing_keys = {t.spec.task_key for t in runtime.tasks.values()}
    for spec in added:
        if spec.task_key in existing_keys and spec.task_key not in superseded:
            continue
        tid = task_id(sid, spec.task_key)
        kind = TaskKind.EXPERIMENT
        route = (spec.route or "").lower()
        if route in ("literature", "lit"):
            kind = TaskKind.LITERATURE
        elif route in ("hypotheses", "hypothesis", "hyp"):
            kind = TaskKind.HYPOTHESES
        elif route in ("planner", "plan"):
            kind = TaskKind.PLAN
        task = Task(
            task_id=tid,
            kind=kind,
            spec=spec,
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
                schema_version=7,
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
        "foreach_refute": (template.spec.tests_intent == "refute"),
    }
    events.append(
        EventEnvelope(
            study_id=sid,
            type="PlanExpanded",
            entity_key=pid,
            # Deterministic: template id only (no random pid).
            idem_key=make_idem("PlanExpanded", "foreach", sid, tkey),
            payload=payload,
            ts=now,
            schema_version=1,
        )
    )
    await runtime._append(events)  # noqa: SLF001
    children = getattr(runtime, "_foreach_children", None)
    if not isinstance(children, dict):
        runtime._foreach_children = {}  # noqa: SLF001
        children = runtime._foreach_children  # noqa: SLF001
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
    items = _items_for_source(runtime, src, fe)
    return await materialize_foreach(runtime, template=tmpl, items=items)


async def maybe_foreach_after_task_done(
    runtime: StudyRuntime, task_id_full: str
) -> list[str]:
    """After source TaskDone, materialize any foreach templates waiting on it."""
    done: list[str] = []
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return done
    src = runtime.tasks.get(task_id_full)
    if src is None or src.state != TaskState.DONE:
        return done
    reason = getattr(src, "reason", None)
    rv = getattr(reason, "value", reason)
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
        except Exception as e:  # noqa: BLE001 — never raise; refuse + skip
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
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return done
    for tmpl in list(foreach_templates(runtime)):
        fe = tmpl.spec.foreach
        after = fe.of_task if fe else tmpl.spec.task_key
        try:
            pid = await _materialize_one(runtime, tmpl)
            if pid:
                done.append(pid)
        except Exception as e:  # noqa: BLE001
            await _refuse_and_skip_foreach(
                runtime,
                tmpl,
                after=after,
                detail=f"foreach_exception:{type(e).__name__}",
            )
    return done


async def resume_pending_foreach(runtime: StudyRuntime) -> int:
    """Re-run foreach after crash / reopen (live == replay)."""
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return 0
    got = await materialize_ready_foreach(runtime)
    return len(got)


def validate_foreach_specs(tasks: list[TaskSpec], *, known_keys: set[str]) -> None:
    """Reject invalid foreach at accept_plan / validate_expansion."""
    keys = {t.task_key for t in tasks} | known_keys
    stage = {"SPEC", "HYP", "LIT", "PLAN", "REPORT"}
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
