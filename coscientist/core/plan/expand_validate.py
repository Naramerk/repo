"""Pre-apply validation for PlanExpanded / ExpansionRequest (M1 PR9 review).

Returns error detail strings; never raises to the tick/record path.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from coscientist.core.events.ids import task_id
from coscientist.core.execution.domain import (
    QuestionSpec,
    QuestionSupersede,
    TaskKind,
    TaskSpec,
)
from coscientist.core.execution.inputs import validate_input_specs, validate_plan_criteria

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime

def _prefix_for_route(route: str) -> str:
    r = (route or "").lower()
    if r in ("literature", "lit"):
        return "LIT"
    if r in ("hypotheses", "hypothesis", "hyp"):
        return "HYP"
    if r in ("planner", "plan"):
        return "PLAN"
    return "EXP"


def task_kind_for_route(route: str) -> TaskKind:
    """Map plan/expansion route → ``TaskKind`` (HYP/LIT/PLAN vs experiment)."""
    prefix = _prefix_for_route(route)
    if prefix == "LIT":
        return TaskKind.LITERATURE
    if prefix == "HYP":
        return TaskKind.HYPOTHESES
    if prefix == "PLAN":
        return TaskKind.PLAN
    return TaskKind.EXPERIMENT


def next_kind_index(runtime: StudyRuntime, prefix: str, tasks: list[TaskSpec]) -> int:
    """Next ``{prefix}-<n>`` index from runtime tasks + candidates."""
    n = 0
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    for rt_task in runtime.tasks.values():
        m = pat.match(rt_task.spec.task_key)
        if m:
            n = max(n, int(m.group(1)) + 1)
    for spec in tasks:
        m = pat.match(spec.task_key)
        if m:
            n = max(n, int(m.group(1)) + 1)
    return n


def assign_core_keys(runtime: StudyRuntime, added: list[TaskSpec]) -> list[TaskSpec] | str:
    """Core assigns EXP/LIT/HYP/PLAN-<n> for all expansion kinds; ignore agent keys."""
    out: list[TaskSpec] = []
    counters: dict[str, int] = {}
    seen: set[str] = {t.spec.task_key for t in runtime.tasks.values()}
    for spec in added:
        prefix = _prefix_for_route(spec.route)
        if prefix not in counters:
            counters[prefix] = next_kind_index(runtime, prefix, [])
        key = f"{prefix}-{counters[prefix]}"
        counters[prefix] += 1
        if key in seen:
            return f"duplicate task_key after assign: {key}"
        seen.add(key)
        out.append(spec.model_copy(update={"task_key": key}))
    return out


# Back-compat alias used by older call sites / tests.
def assign_exp_keys(runtime: StudyRuntime, added: list[TaskSpec]) -> list[TaskSpec] | str:
    return assign_core_keys(runtime, added)


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
    from coscientist.core.plan.foreach import validate_foreach_specs
    from coscientist.core.plan.shares import critic_plan_shares

    try:
        validate_foreach_specs(merged, known_keys=keys)
    except ValueError as e:
        return str(e) or "invalid_foreach"
    share_err = critic_plan_shares(merged, study=study)
    if share_err is not None:
        return share_err
    catalog = getattr(runtime, "catalog", None) or getattr(
        getattr(runtime, "executor", None), "catalog", None
    )
    validate_plan_criteria(merged, catalog)
    # M1 PR13: final_type + rubric immutability (change only via supersede).
    from coscientist.core.plan.plan_validate import validate_plan_final_types_or_err

    # Active questions after supersede (H-f: only new / non-disposed need coverage).
    superseded_q_old = {s.old_id for s in superseded_questions}
    active_qs = (set(active_q_ids) | {q.id for q in added_questions}) - superseded_q_old
    prev_final: dict[str, str] = {}
    store_plans = getattr(runtime, "plans", None)
    plan_rows: list[Any] = []
    if isinstance(store_plans, dict):
        plan_rows = list(store_plans.values())
    for plan in plan_rows:
        qs = plan.get("questions") if isinstance(plan, dict) else getattr(plan, "questions", None)
        for q in qs or []:
            if isinstance(q, dict) and q.get("id"):
                prev_final[str(q["id"])] = str(q.get("final_type") or "verdict")
            elif hasattr(q, "id"):
                prev_final[str(q.id)] = str(getattr(q, "final_type", None) or "verdict")
    q_for_validate: list[QuestionSpec] = list(added_questions)
    seen_q = {q.id for q in q_for_validate}
    for qid, text in texts.items():
        if qid in seen_q or qid not in active_qs:
            continue
        ft = prev_final.get(qid, "verdict")
        q_for_validate.append(
            QuestionSpec(id=qid, text=text, final_type=ft)  # type: ignore[arg-type]
        )
        seen_q.add(qid)
    ft_err = validate_plan_final_types_or_err(
        merged,
        q_for_validate,
        active_question_ids=active_qs if active_qs else None,
        prev_tasks=prev_tasks,
        superseded_tasks=set(superseded_tasks),
        catalog=catalog,
    )
    if ft_err is not None:
        return ft_err
    # max_depth + acyclicity: decomposes via parent_id ∪ graph (DESIGN_M1 §10).
    from coscientist.core.plan.decomposes import assert_decomposes_acyclic, max_decomposes_depth
    from coscientist.core.plan.plan_validate import validate_decomposes

    max_depth = int(getattr(study, "max_depth", 8) or 8)
    parent_of: dict[str, str | None] = {
        q.id: getattr(q, "parent_id", None) for q in added_questions
    }
    # Prior plan questions may already carry parent_id.
    for plan in plan_rows:
        qs = plan.get("questions") if isinstance(plan, dict) else getattr(plan, "questions", None)
        for q in qs or []:
            if isinstance(q, dict) and q.get("id"):
                qid = str(q["id"])
                parent = q.get("parent_id")
                if qid not in parent_of or parent_of[qid] is None:
                    parent_of[qid] = str(parent) if parent else parent_of.get(qid)
            elif hasattr(q, "id"):
                qid = str(q.id)
                parent = getattr(q, "parent_id", None)
                if qid not in parent_of or parent_of[qid] is None:
                    parent_of[qid] = str(parent) if parent else parent_of.get(qid)
    for qid in texts:
        if qid not in parent_of:
            parent_of[qid] = _parent_from_graph(runtime, sid, qid)
        elif parent_of[qid] is None:
            parent_of[qid] = _parent_from_graph(runtime, sid, qid)
    for _qid, parent in list(parent_of.items()):
        if parent and parent not in parent_of:
            parent_of[parent] = _parent_from_graph(runtime, sid, parent)
    try:
        assert_decomposes_acyclic(parent_of)
        depth = max_decomposes_depth(parent_of) if any(parent_of.values()) else 0
    except ValueError as e:
        return str(e) or "max_depth:cycle"
    if depth > max_depth:
        return f"max_depth:{max_depth}"
    if not any(parent_of.values()):
        return None
    try:
        by_id = {q.id: q for q in q_for_validate}
        q_all: list[QuestionSpec] = []
        for qid, parent in parent_of.items():
            existing = by_id.get(qid)
            if existing is not None:
                if parent and not getattr(existing, "parent_id", None):
                    q_all.append(existing.model_copy(update={"parent_id": parent}))
                else:
                    q_all.append(existing)
            else:
                q_all.append(
                    QuestionSpec(
                        id=qid,
                        text=texts.get(qid, ""),
                        parent_id=parent,
                        final_type=prev_final.get(qid, "verdict"),  # type: ignore[arg-type]
                    )
                )
        rollup = "all"
        if plan_rows:
            last = plan_rows[-1] if isinstance(plan_rows[-1], dict) else None
            if isinstance(last, dict) and last.get("rollup") in ("all", "any", "majority", "rubric"):
                rollup = str(last["rollup"])
        validate_decomposes(
            merged,
            q_all,
            rollup=rollup,
            max_depth=max_depth,
            max_tasks=int(getattr(study, "max_tasks", 64) or 64),
        )
    except ValueError as e:
        return str(e) or "invalid_decomposes"
    return None


def _parent_from_graph(runtime: StudyRuntime, sid: str, qid: str) -> str | None:
    from coscientist.core.events.ids import question_node_id

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
    from coscientist.core.claims.claims import filtered_claims_by_target

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
