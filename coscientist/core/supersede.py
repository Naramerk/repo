"""Question/criterion supersede helpers (M1 PR9 / C10 / §11 / MJ-b)."""

from __future__ import annotations

from typing import Any, Literal

from coscientist.core.domain import TaskState
from coscientist.core.ids import (
    criterion_node_id,
    experiment_node_id,
    hypothesis_node_id,
    question_node_id,
)

TestsIntent = Literal["confirm", "refute"]


def plan_node_id(study_id: str, pid: str) -> str:
    return f"{study_id}:plan:{pid}"


def question_is_superseded(graph: Any, study_id: str, question_id: str) -> bool:
    """True when some newer question −supersedes→ this question."""
    if graph is None or not question_id:
        return False
    qnid = question_node_id(study_id, question_id)
    for e in graph.all_edges():
        if e.kind == "supersedes" and e.dst_id == qnid:
            src = graph.get_node(e.src_id)
            if src is not None and src.kind == "question":
                return True
    return False


def criterion_is_superseded(graph: Any, study_id: str, criterion_key: str) -> bool:
    if graph is None or not criterion_key:
        return False
    cnid = criterion_node_id(study_id, criterion_key)
    for e in graph.all_edges():
        if e.kind == "supersedes" and e.dst_id == cnid:
            src = graph.get_node(e.src_id)
            if src is not None and src.kind == "criterion":
                return True
    return False


def countable_refutation_n(runtime: Any, target_id: str) -> int:
    """Count claims that ``counts()`` and are inconsistent for this target."""
    study = getattr(runtime, "study", None)
    graph = getattr(runtime, "graph", None)
    if study is None or graph is None or not target_id:
        return 0
    from coscientist.core.claims import filtered_claims_by_target
    from coscientist.core.verdict import effective_outcome

    by = filtered_claims_by_target(
        graph, study_id=study.study_id, results=getattr(runtime, "results", None)
    )
    claims = list(by.get(f"question:{target_id}") or []) + list(
        by.get(f"hypothesis:{target_id}") or []
    ) + list(by.get(target_id) or [])
    n = 0
    for c in claims:
        if c.counts() and effective_outcome(c) == "inconsistent":
            n += 1
    return n


def derive_tests_intent(runtime: Any, spec: Any) -> TestsIntent | None:
    """Core-derived tests.intent from catalog check roles (not planner alone).

    - Graph / catalog prediction-role criteria are required for ``refute``.
    - Planner ``TaskSpec.tests_intent=refute`` is ignored without a prediction role.
    - ``predicts_null`` templates imply refute.
    """
    planner = getattr(spec, "tests_intent", None)
    catalog = getattr(runtime, "catalog", None)
    by_id = {t.id: t for t in getattr(catalog, "templates", []) or []} if catalog else {}
    has_prediction = False
    predicts_null = False
    for ref in getattr(spec, "criteria", None) or []:
        tid = getattr(ref, "template_id", None)
        tmpl = by_id.get(tid) if tid else None
        role = getattr(tmpl, "role", None) if tmpl is not None else getattr(ref, "role", None)
        if role == "prediction":
            has_prediction = True
        if tmpl is not None and bool(getattr(tmpl, "predicts_null", False)):
            predicts_null = True
            has_prediction = True
    if predicts_null:
        return "refute"
    if planner == "refute":
        return "refute" if has_prediction else None
    if planner == "confirm" and has_prediction:
        return "confirm"
    if has_prediction:
        return "confirm"
    return None


def task_has_refute_intent(runtime: Any, task: Any) -> bool:
    """True when the task carries tests.intent=refute (edge or core-derived)."""
    spec = getattr(task, "spec", task)
    study = getattr(runtime, "study", None)
    graph = getattr(runtime, "graph", None)
    if study is not None and graph is not None:
        key = getattr(spec, "task_key", None) or (
            spec.get("task_key") if isinstance(spec, dict) else None
        )
        if key:
            exp_id = experiment_node_id(study.study_id, str(key))
            for e in graph.edges_from(exp_id):
                if e.kind == "tests" and (e.attrs or {}).get("intent") == "refute":
                    return True
    return derive_tests_intent(runtime, spec) == "refute"


def has_open_refute_task(runtime: Any, target_id: str) -> bool:
    """True when a non-DONE task has tests.intent=refute toward this target.

    PENDING tasks without an experiment node are protected via core-derived
    ``tests_intent`` from check roles.
    """
    study = getattr(runtime, "study", None)
    graph = getattr(runtime, "graph", None)
    if study is None or not target_id:
        return False
    sid = study.study_id
    hid = hypothesis_node_id(sid, target_id)
    for task in runtime.tasks.values():
        if task.state == TaskState.DONE:
            continue
        if not task_has_refute_intent(runtime, task):
            continue
        addrs = list(task.spec.addresses or [])
        if graph is not None:
            exp_id = experiment_node_id(sid, task.spec.task_key)
            for e in graph.edges_from(exp_id):
                if e.kind == "tests" and (e.attrs or {}).get("intent") == "refute":
                    if e.dst_id == hid:
                        return True
        if target_id in addrs:
            return True
    return False


def target_blocks_agent_supersede(runtime: Any, target_id: str) -> str | None:
    """Agent-path block reason, or None if supersede/SKIP is allowed."""
    if countable_refutation_n(runtime, target_id) >= 1:
        return "has_refutation"
    if has_open_refute_task(runtime, target_id):
        return "open_refute_task"
    return None


def question_has_countable_refutation(runtime: Any, question_id: str) -> bool:
    return target_blocks_agent_supersede(runtime, question_id) is not None


def tasks_needed_by_active_questions(
    *,
    tasks: list[Any],
    superseded_old: set[str],
    active_q_after: set[str],
) -> set[str]:
    """Task keys that active (non-superseded / new) questions' tasks depend on."""
    by_key: dict[str, Any] = {}
    for t in tasks:
        key = getattr(t, "task_key", None) or (t.get("task_key") if isinstance(t, dict) else None)
        if key:
            by_key[str(key)] = t

    def _addrs(t: Any) -> set[str]:
        raw = getattr(t, "addresses", None)
        if raw is None and isinstance(t, dict):
            raw = t.get("addresses")
        return {str(a) for a in (raw or [])}

    def _deps(t: Any) -> list[str]:
        raw = getattr(t, "depends_on", None)
        if raw is None and isinstance(t, dict):
            raw = t.get("depends_on")
        return [str(d) for d in (raw or [])]

    keepers = {k for k, t in by_key.items() if _addrs(t) & active_q_after}
    for k, t in by_key.items():
        addrs = _addrs(t)
        if addrs and not (addrs <= superseded_old):
            keepers.add(k)

    needed: set[str] = set(keepers)
    changed = True
    while changed:
        changed = False
        for k in list(needed):
            t = by_key.get(k)
            if t is None:
                continue
            for d in _deps(t):
                if d not in needed and d in by_key:
                    needed.add(d)
                    changed = True
    return needed


def should_skip_for_question_supersede(
    *,
    task: Any,
    superseded_old: set[str],
    active_q_after: set[str],
    protected_keys: set[str],
    blocked_questions: set[str] | None = None,
    refute_keys: set[str] | None = None,
) -> bool:
    """True when an open task solely addresses superseded questions and is not shared."""
    key = getattr(task, "task_key", None) or (
        task.get("task_key") if isinstance(task, dict) else None
    )
    if key and str(key) in protected_keys:
        return False
    if key and refute_keys and str(key) in refute_keys:
        return False
    raw = getattr(task, "addresses", None)
    if raw is None and isinstance(task, dict):
        raw = task.get("addresses")
    addrs = {str(a) for a in (raw or [])}
    if not addrs:
        return False
    if blocked_questions and (addrs & blocked_questions):
        return False
    if addrs <= superseded_old and not (addrs & active_q_after):
        return True
    return False
