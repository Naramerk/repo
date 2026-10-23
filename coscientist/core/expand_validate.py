"""Pre-apply validation for PlanExpanded / ExpansionRequest (M1 PR9 review).

Returns error detail strings; never raises to the tick/record path.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from coscientist.core.domain import QuestionSpec, QuestionSupersede, TaskSpec
from coscientist.core.ids import task_id
from coscientist.core.inputs import validate_input_specs, validate_plan_criteria

if TYPE_CHECKING:
    from coscientist.core.machine import StudyRuntime

_EXP_KEY = re.compile(r"^EXP-(\d+)$")


def next_exp_index(runtime: StudyRuntime, tasks: list[TaskSpec]) -> int:
    """Next EXP-<n> index from runtime tasks + candidate keys."""
    n = 0
    for rt_task in runtime.tasks.values():
        m = _EXP_KEY.match(rt_task.spec.task_key)
        if m:
            n = max(n, int(m.group(1)) + 1)
    for spec in tasks:
        m = _EXP_KEY.match(spec.task_key)
        if m:
            n = max(n, int(m.group(1)) + 1)
    return n


def assign_exp_keys(runtime: StudyRuntime, added: list[TaskSpec]) -> list[TaskSpec] | str:
    """Core assigns EXP-<n> for experiment routes; ignore agent keys; reject dupes."""
    out: list[TaskSpec] = []
    idx = next_exp_index(runtime, [])
    seen: set[str] = {t.spec.task_key for t in runtime.tasks.values()}
    for spec in added:
        route = (spec.route or "").lower()
        if route in ("experiment", "exp") or _EXP_KEY.match(spec.task_key):
            key = f"EXP-{idx}"
            idx += 1
            if key in seen:
                return f"duplicate task_key after assign: {key}"
            seen.add(key)
            out.append(spec.model_copy(update={"task_key": key}))
        else:
            if spec.task_key in seen:
                return f"duplicate task_key: {spec.task_key}"
            seen.add(spec.task_key)
            out.append(spec)
    return out


def validate_expansion(
    runtime: StudyRuntime,
    *,
    added_tasks: list[TaskSpec],
    added_questions: list[QuestionSpec],
    superseded_tasks: list[str],
    superseded_questions: list[QuestionSupersede],
    known_texts: dict[str, str],
    prev_tasks: list[TaskSpec],
    active_q_ids: set[str],
) -> str | None:
    """Return detail string on invalid request, else None."""
    try:
        return _validate(
            runtime,
            added_tasks=added_tasks,
            added_questions=added_questions,
            superseded_tasks=superseded_tasks,
            superseded_questions=superseded_questions,
            known_texts=known_texts,
            prev_tasks=prev_tasks,
            active_q_ids=active_q_ids,
        )
    except ValueError as e:
        return str(e) or "invalid"
    except Exception as e:  # noqa: BLE001 — totality for tick path
        return f"{type(e).__name__}:{e}"


def _validate(
    runtime: StudyRuntime,
    *,
    added_tasks: list[TaskSpec],
    added_questions: list[QuestionSpec],
    superseded_tasks: list[str],
    superseded_questions: list[QuestionSupersede],
    known_texts: dict[str, str],
    prev_tasks: list[TaskSpec],
    active_q_ids: set[str],
) -> str | None:
    study = runtime.study
    if study is None:
        return "no_study"
    sid = study.study_id
    added_ids = {q.id for q in added_questions}
    texts = dict(known_texts)
    for q in added_questions:
        if q.id in texts and texts[q.id] != q.text:
            return f"question id {q.id!r} text conflict"
        texts[q.id] = q.text
    prev_q_ids = set(known_texts)
    for s in superseded_questions:
        if s.old_id not in texts and s.old_id not in prev_q_ids:
            return f"unknown superseded question id: {s.old_id!r}"
        if s.new_id not in added_ids and s.new_id not in texts:
            return f"supersede new_id not in added_questions: {s.new_id!r}"
    skip = set(superseded_tasks)
    kept = [t for t in prev_tasks if t.task_key not in skip]
    merged = kept + list(added_tasks)
    if len(merged) > study.max_tasks:
        return "max_tasks"
    # Allow deps on existing runtime DONE/PENDING keys (not only merged revision).
    runtime_keys = {t.spec.task_key for t in runtime.tasks.values()}
    keys = {t.task_key for t in merged} | runtime_keys
    stage = {"SPEC", "HYP", "LIT", "PLAN"}
    for t in merged:
        for d in t.depends_on:
            if d in keys or d in stage or d.startswith("PLAN-"):
                continue
            return f"unknown dependency {d}"
    by = {t.task_key: list(t.depends_on) for t in merged}
    seen: set[str] = set()

    def visit(k: str, stack: set[str]) -> None:
        if k in stack:
            raise ValueError("cyclic plan DAG")
        if k in seen or k not in by:
            return
        stack.add(k)
        for d in by[k]:
            visit(d, stack)
        stack.remove(k)
        seen.add(k)

    for k in by:
        visit(k, set())
    for t in merged:
        for qid in t.addresses:
            if qid not in texts and qid not in active_q_ids:
                return f"unknown question id in addresses: {qid!r}"
    validate_input_specs(merged)
    catalog = getattr(runtime, "catalog", None) or getattr(
        getattr(runtime, "executor", None), "catalog", None
    )
    validate_plan_criteria(merged, catalog)
    # max_depth: decomposes chain via QuestionSpec.parent_id (optional).
    max_depth = int(getattr(study, "max_depth", 8) or 8)
    parent_of = {q.id: getattr(q, "parent_id", None) for q in added_questions}
    for _qid, parent in parent_of.items():
        if not parent:
            continue
        depth = 1
        walk = parent
        guard = 0
        while walk:
            depth += 1
            if depth > max_depth:
                return f"max_depth:{max_depth}"
            walk = parent_of.get(walk) or _parent_from_graph(runtime, sid, walk)
            guard += 1
            if guard > max_depth + 2:
                return "max_depth:cycle"
    return None


def _parent_from_graph(runtime: StudyRuntime, sid: str, qid: str) -> str | None:
    from coscientist.core.ids import question_node_id

    graph = getattr(runtime, "graph", None)
    if graph is None:
        return None
    nid = question_node_id(sid, qid)
    for e in graph.edges_from(nid) if hasattr(graph, "edges_from") else []:
        if e.kind == "decomposes":
            dst = graph.get_node(e.dst_id)
            if dst is not None and dst.kind == "question":
                prefix = f"{sid}:question:"
                return e.dst_id[len(prefix) :] if e.dst_id.startswith(prefix) else (dst.ref_id or None)
    # decomposes: child → parent (src=child, dst=parent) or reverse — check edges_to.
    for e in graph.all_edges() if hasattr(graph, "all_edges") else []:
        if e.kind != "decomposes":
            continue
        if e.src_id == nid:
            dst = graph.get_node(e.dst_id)
            if dst is not None and dst.kind == "question":
                prefix = f"{sid}:question:"
                return e.dst_id[len(prefix) :] if e.dst_id.startswith(prefix) else (dst.ref_id or None)
    return None


def task_produced_countable_claims(runtime: StudyRuntime, tkey: str) -> bool:
    """True when a DONE task produced at least one claim that still counts."""
    sid = runtime.study.study_id if runtime.study else ""
    tid = task_id(sid, tkey) if sid else tkey
    task = runtime.tasks.get(tid)
    if task is None:
        return False
    from coscientist.core.claims import filtered_claims_by_target

    graph = getattr(runtime, "graph", None)
    if graph is None:
        return False
    by = filtered_claims_by_target(graph, study_id=sid, results=runtime.results)
    for claims in by.values():
        for c in claims:
            if getattr(c, "attempt_id", "").startswith(tid) and c.counts():
                return True
    return False


def active_plan_blob(runtime: StudyRuntime) -> dict[str, Any] | None:
    if not runtime.plans:
        return None
    return max(runtime.plans.values(), key=lambda p: int(p.get("revision") or 0))
