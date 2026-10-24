"""Pre-apply validation for PlanExpanded / ExpansionRequest (M1 PR9 review).

Returns error detail strings; never raises to the tick/record path.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from coscientist.core.claims.rubric_types import VALID_FINAL_TYPES, FinalType
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.ids import task_id
from coscientist.core.events.upcasters import require_exact_int
from coscientist.core.execution.domain import (
    STAGE_DEP_KEYS,
    PlanRole,
    QuestionSpec,
    QuestionSupersede,
    Route,
    TaskKind,
    TaskSpec,
    UnknownRouteError,
    parse_route,
)
from coscientist.core.execution.inputs import validate_input_specs, validate_plan_criteria
from coscientist.core.plan.decomposes import VALID_ROLLUP_RULES, RollupRule

if TYPE_CHECKING:
    from coscientist.core.execution.machine import StudyRuntime


class MaxDepthExceeded(ValueError):
    """Plan / expansion exceeds Study.max_depth — maps to ExpansionRefused(expansion_limit)."""

    def __init__(self, max_depth: int) -> None:
        self.max_depth = max_depth
        super().__init__(f"max_depth:{max_depth}")


def _require_final_type(raw: object, *, qid: str) -> FinalType:
    if not isinstance(raw, str) or raw not in VALID_FINAL_TYPES:
        raise ValueError(f"invalid_final_type:{qid}:{raw!r}")
    return raw  # type: ignore[return-value]


def _require_rollup(raw: object) -> RollupRule:
    if not isinstance(raw, str) or raw not in VALID_ROLLUP_RULES:
        raise ValueError(f"invalid_rollup:{raw!r}")
    return raw  # type: ignore[return-value]


def _rollup_from_plans(plan_rows: list[Any]) -> tuple[RollupRule | None, str | None]:
    """Return ``(rollup, None)`` or ``(None, error_detail)`` from the latest plan."""
    if not plan_rows:
        return None, "missing_plan_rollup"
    last = plan_rows[-1]
    try:
        if isinstance(last, dict):
            if "rollup" not in last:
                return None, "missing_plan_rollup"
            return _require_rollup(last["rollup"]), None
        return _require_rollup(last.rollup), None
    except ValueError as e:
        return None, str(e)


def _prefix_for_route(route: object) -> str:
    raw = route.value if isinstance(route, Route) else route
    try:
        return parse_route(raw).key_prefix
    except UnknownRouteError:
        raise


def task_kind_for_route(route: object) -> TaskKind:
    """Map canonical ``TaskSpec.route`` to ``TaskKind`` via ``Route`` enum."""
    raw = route.value if isinstance(route, Route) else route
    try:
        return parse_route(raw).task_kind
    except UnknownRouteError:
        raise


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
        try:
            prefix = _prefix_for_route(spec.route)
        except UnknownRouteError as e:
            return str(e)
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
    """Return detail on invalid request, else None.

    Raises ``MaxDepthExceeded`` (typed expansion_limit) — callers must not
    treat it as a returned detail object.
    """
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
    except MaxDepthExceeded:
        raise
    except ValueError as e:
        msg = str(e)
        if not msg:
            raise
        return msg


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
    plan_keys = {
        t.spec.task_key
        for t in runtime.tasks.values()
        if t.plan_role in (PlanRole.BOOTSTRAP, PlanRole.PLAN_N)
    }
    keys = {t.task_key for t in merged} | runtime_keys
    for t in merged:
        for d in t.depends_on:
            if d in keys or d in STAGE_DEP_KEYS or d in plan_keys:
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
        msg = str(e)
        return msg if msg else "invalid_foreach"
    share_err = critic_plan_shares(merged, study=study)
    if share_err is not None:
        return share_err
    catalog = runtime.catalog
    if catalog is None:
        executor = runtime.executor
        catalog = executor.catalog if executor is not None else None
    validate_plan_criteria(merged, catalog)
    # M1 PR13: final_type + rubric immutability (change only via supersede).
    from coscientist.core.plan.plan_validate import validate_plan_final_types_or_err

    # Active questions after supersede (H-f: only new / non-disposed need coverage).
    superseded_q_old = {s.old_id for s in superseded_questions}
    active_qs = (set(active_q_ids) | {q.id for q in added_questions}) - superseded_q_old
    prev_final: dict[str, FinalType] = {}
    plan_rows: list[Any] = list(runtime.plans.values()) if runtime.plans else []
    for plan in plan_rows:
        qs = plan["questions"] if isinstance(plan, dict) else plan.questions
        for q in qs or []:
            if isinstance(q, dict):
                if "id" not in q:
                    return "plan_question_missing_id"
                qid = str(q["id"])
                if "final_type" not in q:
                    return f"missing_final_type:{qid}"
                try:
                    prev_final[qid] = _require_final_type(q["final_type"], qid=qid)
                except ValueError as e:
                    return str(e)
            else:
                qid = str(q.id)
                try:
                    prev_final[qid] = _require_final_type(q.final_type, qid=qid)
                except ValueError as e:
                    return str(e)
    q_for_validate: list[QuestionSpec] = list(added_questions)
    seen_q = {q.id for q in q_for_validate}
    for qid, text in texts.items():
        if qid in seen_q or qid not in active_qs:
            continue
        if qid not in prev_final:
            return f"missing_final_type:{qid}"
        q_for_validate.append(
            QuestionSpec(id=qid, text=text, final_type=prev_final[qid])
        )
        seen_q.add(qid)
    max_depth = int(study.max_depth)
    max_tasks = int(study.max_tasks)
    needs_decomposes = any(q.parent_id is not None for q in q_for_validate)
    rollup: RollupRule | None = None
    if needs_decomposes:
        rollup, rollup_err = _rollup_from_plans(plan_rows)
        if rollup_err is not None:
            return rollup_err
    ft_kwargs: dict[str, Any] = {
        "active_question_ids": active_qs if active_qs else None,
        "prev_tasks": prev_tasks,
        "superseded_tasks": set(superseded_tasks),
        "catalog": catalog,
        "max_depth": max_depth,
        "max_tasks": max_tasks,
    }
    if rollup is not None:
        ft_kwargs["rollup"] = rollup
    ft_err = validate_plan_final_types_or_err(merged, q_for_validate, **ft_kwargs)
    if ft_err is not None:
        return ft_err
    # max_depth + acyclicity: decomposes via parent_id ∪ graph (DESIGN_M1 §10).
    from coscientist.core.plan.decomposes import assert_decomposes_acyclic, max_decomposes_depth
    from coscientist.core.plan.plan_validate import validate_decomposes
    parent_of: dict[str, str | None] = {q.id: q.parent_id for q in added_questions}
    # Prior plan questions may already carry parent_id.
    for plan in plan_rows:
        qs = plan["questions"] if isinstance(plan, dict) else plan.questions
        for q in qs or []:
            if isinstance(q, dict):
                if "id" not in q:
                    return "plan_question_missing_id"
                qid = str(q["id"])
                parent = q.get("parent_id")
                if qid not in parent_of or parent_of[qid] is None:
                    parent_of[qid] = str(parent) if parent else parent_of.get(qid)
            else:
                qid = str(q.id)
                parent = q.parent_id
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
        msg = str(e)
        if not msg:
            raise
        return msg
    if depth > max_depth:
        raise MaxDepthExceeded(max_depth)
    if not any(parent_of.values()):
        return None
    try:
        by_id = {q.id: q for q in q_for_validate}
        q_all: list[QuestionSpec] = []
        for qid, parent in parent_of.items():
            existing = by_id.get(qid)
            if existing is not None:
                if parent and existing.parent_id is None:
                    q_all.append(existing.model_copy(update={"parent_id": parent}))
                else:
                    q_all.append(existing)
            else:
                if qid not in prev_final:
                    return f"missing_final_type:{qid}"
                if qid not in texts:
                    return f"missing_question_text:{qid}"
                q_all.append(
                    QuestionSpec(
                        id=qid,
                        text=texts[qid],
                        parent_id=parent,
                        final_type=prev_final[qid],
                    )
                )
        if rollup is None:
            rollup, rollup_err = _rollup_from_plans(plan_rows)
            if rollup_err is not None:
                return rollup_err
        assert rollup is not None
        validate_decomposes(
            merged,
            q_all,
            rollup=rollup,
            max_depth=max_depth,
            max_tasks=max_tasks,
        )
    except ValueError as e:
        msg = str(e)
        if not msg:
            raise
        return msg
    return None


def _question_ref_from_edge(runtime: StudyRuntime, sid: str, dst_id: str) -> str:
    from coscientist.core.events.ids import question_node_id

    dst = runtime.graph.get_node(dst_id)
    if dst is None:
        raise JournalFormatError(f"decomposes edge dst missing: {dst_id!r}")
    if dst.kind != "question":
        raise JournalFormatError(f"decomposes dst not question: {dst_id!r}")
    if isinstance(dst.ref_id, str) and dst.ref_id:
        return dst.ref_id
    prefix = f"{sid}:question:"
    expected = question_node_id(sid, "")
    _ = expected
    if not dst_id.startswith(prefix):
        raise JournalFormatError(f"decomposes dst id layout invalid: {dst_id!r}")
    qid = dst_id[len(prefix) :]
    if not qid:
        raise JournalFormatError(f"decomposes dst empty question id: {dst_id!r}")
    return qid


def _parent_from_graph(runtime: StudyRuntime, sid: str, qid: str) -> str | None:
    from coscientist.core.events.ids import question_node_id

    graph = runtime.graph
    if graph is None:
        raise JournalFormatError("runtime.graph required for parent walk")
    nid = question_node_id(sid, qid)
    for e in graph.edges_from(nid):
        if e.kind == "decomposes":
            return _question_ref_from_edge(runtime, sid, e.dst_id)
    if not hasattr(graph, "all_edges"):
        raise JournalFormatError("graph missing all_edges")
    for e in graph.all_edges():
        if e.kind != "decomposes":
            continue
        if e.src_id == nid:
            return _question_ref_from_edge(runtime, sid, e.dst_id)
    return None


def task_produced_countable_claims(runtime: StudyRuntime, tkey: str) -> bool:
    """True when a DONE task produced at least one claim that still counts."""
    if runtime.study is None:
        raise JournalFormatError("task_produced_countable_claims: missing study")
    sid = runtime.study.study_id
    tid = task_id(sid, tkey)
    task = runtime.tasks.get(tid)
    if task is None:
        return False
    from coscientist.core.claims.claims import filtered_claims_by_target

    by = filtered_claims_by_target(runtime.graph, study_id=sid, results=runtime.results)
    for claims in by.values():
        for c in claims:
            aid = getattr(c, "attempt_id", None)
            # Empty/missing attempt_id → not countable for this task (skip, no invent).
            if not isinstance(aid, str) or not aid:
                continue
            # Structured attempt ids are ``{task_id}#{n}`` — not free substring match.
            if aid.startswith(f"{tid}#") and c.counts():
                return True
    return False


def active_plan_blob(runtime: StudyRuntime) -> dict[str, Any] | None:
    if not runtime.plans:
        return None
    return max(
        runtime.plans.values(),
        key=lambda p: require_exact_int(p["revision"], what="plan.revision"),
    )
