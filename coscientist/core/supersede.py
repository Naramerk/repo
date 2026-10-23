"""Question/criterion supersede helpers (M1 PR9 / C10 / §11)."""

from __future__ import annotations

from typing import Any

from coscientist.core.ids import criterion_node_id, question_node_id


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


def question_has_countable_refutation(
    runtime: Any,
    question_id: str,
) -> bool:
    """True when the question has ≥ k_ref countable inconsistent claims (refuted)."""
    study = getattr(runtime, "study", None)
    graph = getattr(runtime, "graph", None)
    if study is None or graph is None or not question_id:
        return False
    from coscientist.core.claims import filtered_claims_by_target
    from coscientist.core.verdict import effective_outcome

    by = filtered_claims_by_target(
        graph, study_id=study.study_id, results=getattr(runtime, "results", None)
    )
    claims = list(by.get(f"question:{question_id}") or []) + list(by.get(question_id) or [])
    k_ref = int(getattr(study, "refutations_needed", 1) or 1)
    n = 0
    for c in claims:
        if getattr(c, "exclusion_reason", None) not in (None, "superseded"):
            continue
        if not (
            getattr(c, "accepted", False)
            and getattr(c, "validity_ok", False)
            and getattr(c, "frozen", False)
            and getattr(c, "provenance", "") == "real"
        ):
            continue
        if getattr(c, "is_lit", False):
            continue
        if effective_outcome(c) == "inconsistent":
            n += 1
    return n >= k_ref


def tasks_needed_by_active_questions(
    *,
    tasks: list[Any],
    superseded_old: set[str],
    active_q_after: set[str],
) -> set[str]:
    """Task keys that active (non-superseded / new) questions' tasks depend on.

    DESIGN_M1 §11: do not SKIP a task that a task of a non-superseded or new
    question depends on (shared-descendant / shared ancestor in the DAG).
    """
    # Map task_key → addresses / depends_on
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

    # Tasks that address an active question.
    keepers = {k for k, t in by_key.items() if _addrs(t) & active_q_after}
    # Also tasks that address only non-superseded questions (no supersede at all).
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
) -> bool:
    """True when an open task solely addresses superseded questions and is not shared."""
    key = getattr(task, "task_key", None) or (
        task.get("task_key") if isinstance(task, dict) else None
    )
    if key and str(key) in protected_keys:
        return False
    raw = getattr(task, "addresses", None)
    if raw is None and isinstance(task, dict):
        raw = task.get("addresses")
    addrs = {str(a) for a in (raw or [])}
    if not addrs:
        return False
    if addrs <= superseded_old and not (addrs & active_q_after):
        return True
    return False
