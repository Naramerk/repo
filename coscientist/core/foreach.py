"""Foreach materialization at source TaskDone (M1 PR10 / S9).

Expands ``TaskSpec.foreach`` into N concrete tasks without planner or expansion
quota. Journaled as ``PlanExpanded`` with ``foreach=true`` (replay == live).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coscientist.core.domain import (
    ForEachSpec,
    InputSpec,
    Plan,
    QuestionSpec,
    Task,
    TaskKind,
    TaskSpec,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.expand_graph import (
    plan_graph_events,
    question_graph_events,
    skip_superseded_task_events,
)
from coscientist.core.expand_validate import active_plan_blob, assign_core_keys
from coscientist.core.ids import plan_id, task_id
from coscientist.core.reasons import TaskReason

if TYPE_CHECKING:
    from coscientist.core.machine import StudyRuntime


def foreach_templates(runtime: StudyRuntime) -> list[Task]:
    """PENDING tasks whose spec.foreach is set."""
    out: list[Task] = []
    for t in runtime.tasks.values():
        if t.state == TaskState.DONE:
            continue
        fe = t.spec.foreach
        if fe is not None:
            out.append(t)
    return out


def select_foreach_items(
    runtime: StudyRuntime,
    fe: ForEachSpec,
) -> list[str]:
    """Logical ids of hypothesis/question nodes matching the selector."""
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
        # Prefer nodes stamped from of_task; else accept unstamped (test upserts).
        src = attrs.get("source_task") or attrs.get("of_task")
        if src is not None and str(src) != fe.of_task:
            continue
        logical = str(node.ref_id or attrs.get("id") or "")
        if not logical:
            # Fall back to suffix of namespaced id.
            nid = str(node.node_id)
            logical = nid.rsplit(":", 1)[-1] if ":" in nid else nid
        if not logical:
            continue
        if fe.selector == "selected" and not bool(attrs.get("selected", False)):
            continue
        items.append((logical, attrs))
    # Stable order: rank then id.
    items.sort(key=lambda x: (int(x[1].get("rank") or 0), x[0]))
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


def expand_input_specs_foreach(runtime: Any, specs: list[InputSpec]) -> list[InputSpec]:
    """Replace inputs.from_task that name a foreach template with its children."""
    children = getattr(runtime, "_foreach_children", None) or {}
    if not children:
        return list(specs)
    out: list[InputSpec] = []
    for inp in specs:
        kids = children.get(inp.from_task)
        if not kids:
            # Template expanded to 0 items → drop this input (quorum sees absence).
            if inp.from_task in children:
                continue
            out.append(inp)
            continue
        for k in kids:
            out.append(inp.model_copy(update={"from_task": k}))
    return out


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


async def materialize_foreach(
    runtime: StudyRuntime,
    *,
    template: Task,
    items: list[str],
) -> str | None:
    """Write PlanExpanded(foreach=true). Returns plan_id or None if closed."""
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return None
    study = runtime.study
    sid = study.study_id
    now = runtime.clock.now()
    tkey = template.spec.task_key
    if foreach_already_done(runtime, tkey):
        return None

    prototypes = [
        _concrete_from_template(template.spec, item_id=item, task_key=f"TMP-{i}") for i, item in enumerate(items)
    ]
    keyed = assign_core_keys(runtime, prototypes) if prototypes else []
    if isinstance(keyed, str):
        # Should not happen with TMP placeholders; refuse silently via empty + skip.
        keyed = []
    added: list[TaskSpec] = list(keyed)

    prev = active_plan_blob(runtime) or {}
    prev_rev = int(prev.get("revision") or 0)
    prev_pid = str(prev.get("plan_id") or "")
    new_rev = prev_rev + 1
    prev_tasks = [TaskSpec.model_validate(t) if isinstance(t, dict) else t for t in (prev.get("tasks") or [])]
    prev_questions = [
        QuestionSpec.model_validate(q) if isinstance(q, dict) else q for q in (prev.get("questions") or [])
    ]

    new_keys = [t.task_key for t in added]
    superseded = [tkey]
    still_kept = [t for t in prev_tasks if t.task_key != tkey]
    merged = still_kept + added
    pid = plan_id()
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
    events.extend(skip_superseded_task_events(runtime, sid=sid, superseded_tasks=superseded, now=now))
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

    fe = template.spec.foreach
    payload = {
        **plan.model_dump(mode="json"),
        "after_task": fe.of_task if fe else tkey,
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
    }
    events.append(
        EventEnvelope(
            study_id=sid,
            type="PlanExpanded",
            entity_key=pid,
            idem_key=make_idem("PlanExpanded", pid, "foreach", tkey),
            payload=payload,
            ts=now,
            schema_version=1,
        )
    )
    await runtime._append(events)  # noqa: SLF001
    # Index children for readiness / tests (also rebuilt on fold).
    children = getattr(runtime, "_foreach_children", None)
    if not isinstance(children, dict):
        runtime._foreach_children = {}  # noqa: SLF001
        children = runtime._foreach_children  # noqa: SLF001
    children[tkey] = list(new_keys)
    return pid


async def maybe_foreach_after_task_done(runtime: StudyRuntime, task_id_full: str) -> list[str]:
    """After source TaskDone, materialize any foreach templates waiting on it."""
    done: list[str] = []
    try:
        if runtime.study is None or getattr(runtime, "_finalized", False):
            return done
        src = runtime.tasks.get(task_id_full)
        if src is None or src.state != TaskState.DONE:
            return done
        # Superseded templates are not sources.
        reason = getattr(src, "reason", None)
        rv = getattr(reason, "value", reason)
        if rv == TaskReason.SUPERSEDED.value:
            return done
        src_key = src.spec.task_key
        for tmpl in foreach_templates(runtime):
            fe = tmpl.spec.foreach
            if fe is None or fe.of_task != src_key:
                continue
            if foreach_already_done(runtime, tmpl.spec.task_key):
                continue
            # Only expand after SUCCESS/PARTIAL source (FAILED → empty items + skip).
            items: list[str] = []
            if src.outcome and src.outcome.value in ("SUCCESS", "PARTIAL"):
                items = select_foreach_items(runtime, fe)
            pid = await materialize_foreach(runtime, template=tmpl, items=items)
            if pid:
                done.append(pid)
    except Exception:  # noqa: BLE001 — never raise out of tick/record
        return done
    return done


async def resume_pending_foreach(runtime: StudyRuntime) -> int:
    """Re-run foreach after crash between source TaskDone and materialize."""
    if runtime.study is None or getattr(runtime, "_finalized", False):
        return 0
    n = 0
    for tid, task in list(runtime.tasks.items()):
        if task.state != TaskState.DONE:
            continue
        reason = getattr(task, "reason", None)
        rv = getattr(reason, "value", reason)
        if rv == TaskReason.SUPERSEDED.value:
            continue
        # Any PENDING foreach waiting on this key?
        waiting = [
            t for t in foreach_templates(runtime) if t.spec.foreach and t.spec.foreach.of_task == task.spec.task_key
        ]
        if not waiting:
            continue
        got = await maybe_foreach_after_task_done(runtime, tid)
        n += len(got)
    return n


def validate_foreach_specs(tasks: list[TaskSpec], *, known_keys: set[str]) -> None:
    """Reject invalid foreach at accept_plan."""
    keys = {t.task_key for t in tasks} | known_keys
    for t in tasks:
        fe = t.foreach
        if fe is None:
            continue
        if t.foreach_item is not None:
            raise ValueError(f"foreach template {t.task_key!r} must not set foreach_item")
        if not fe.of_task:
            raise ValueError(f"foreach.of_task required (task {t.task_key!r})")
        if fe.of_task not in keys and fe.of_task not in {
            "SPEC",
            "HYP",
            "LIT",
            "PLAN",
            "REPORT",
        }:
            # Allow stage keys always present after create_study.
            raise ValueError(f"foreach.of_task {fe.of_task!r} unknown (task {t.task_key!r})")
        if fe.of_task not in t.depends_on:
            raise ValueError(f"foreach template {t.task_key!r} must depend_on {fe.of_task!r}")

