"""Decomposes tree helpers + acyclicity (M1 PR16 / DESIGN_M1 §10).

``decomposes`` edges are child → parent (card out 0..1). Parent links also
live on ``QuestionSpec.parent_id`` in PlanAccepted / PlanExpanded payloads.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from math import ceil
from typing import Any, Literal

DecomposeOrigin = Literal["explicit", "implicit", "emergent"]
VALID_DECOMPOSE_ORIGINS: frozenset[str] = frozenset({"explicit", "implicit", "emergent"})

RollupRule = Literal["all", "any", "majority", "rubric"]
VALID_ROLLUP_RULES: frozenset[str] = frozenset({"all", "any", "majority", "rubric"})

# Frozen PLAN ReviewCheck / rubric item id (DESIGN_M1 §10 (б)).
DECOMPOSITION_COVERS_ITEM_ID = "decomposition_covers"
DECOMPOSITION_COVERS_TEXT = "декомпозиция покрывает исходный вопрос"


def parent_map_from_questions(
    questions: list[Any] | list[Mapping[str, Any]],
) -> dict[str, str | None]:
    """question_id → parent_id (None for roots)."""
    out: dict[str, str | None] = {}
    for q in questions:
        if isinstance(q, Mapping):
            qid = str(q.get("id") or "")
            parent = q.get("parent_id")
        else:
            qid = str(getattr(q, "id", "") or "")
            parent = getattr(q, "parent_id", None)
        if not qid:
            continue
        out[qid] = str(parent) if parent else None
    return out


def parent_map_from_plans(
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> dict[str, str | None]:
    """Union parent_id across plan revisions (first non-None parent wins)."""
    found: dict[str, str | None] = {}
    rows = plans.values() if isinstance(plans, Mapping) else plans
    for plan in rows:
        qs = plan.get("questions") if isinstance(plan, Mapping) else None
        if not qs:
            continue
        for qid, parent in parent_map_from_questions(list(qs)).items():
            if qid not in found:
                found[qid] = parent
            elif found[qid] is None and parent is not None:
                found[qid] = parent
    return found


def children_map(parent_of: Mapping[str, str | None]) -> dict[str, list[str]]:
    """parent_id → sorted child ids."""
    kids: dict[str, list[str]] = defaultdict(list)
    for qid, parent in parent_of.items():
        if parent:
            kids[str(parent)].append(qid)
    return {p: sorted(cs) for p, cs in kids.items()}


def leaf_ids(parent_of: Mapping[str, str | None]) -> list[str]:
    """Questions with no children (includes isolated roots)."""
    kids = children_map(parent_of)
    return sorted(qid for qid in parent_of if qid not in kids)


def assert_decomposes_acyclic(parent_of: Mapping[str, str | None]) -> None:
    """Raise ValueError on self-loop or cycle (mirrors ``_validate_dag``)."""
    for qid, parent in parent_of.items():
        if parent is None:
            continue
        if parent == qid:
            raise ValueError(f"decomposes self-loop on {qid!r}")
        if parent not in parent_of and parent not in {p for p in parent_of.values() if p}:
            # Unknown parent is a separate error; cycle walk still needs known nodes.
            pass
    seen: set[str] = set()

    def visit(k: str, stack: set[str]) -> None:
        if k in stack:
            raise ValueError(f"cyclic decomposes at {k!r}")
        if k in seen:
            return
        stack.add(k)
        parent = parent_of.get(k)
        if parent:
            # Walk even if parent is not yet a key (edge to known root id).
            if parent in parent_of or parent in stack:
                visit(parent, stack)
            else:
                # Parent outside map: treat as root terminator.
                pass
        stack.remove(k)
        seen.add(k)

    for qid in parent_of:
        visit(qid, set())


def decomposes_depth(parent_of: Mapping[str, str | None], qid: str) -> int:
    """Depth of ``qid`` (root = 1). Raises on cycle."""
    depth = 1
    walk: str | None = qid
    guard = 0
    seen: set[str] = set()
    while walk is not None:
        parent = parent_of.get(walk)
        if not parent:
            return depth
        if parent in seen or parent == walk:
            raise ValueError(f"cyclic decomposes at {walk!r}")
        seen.add(walk)
        walk = parent
        depth += 1
        guard += 1
        if guard > len(parent_of) + 2:
            raise ValueError("cyclic decomposes")
    return depth


def max_decomposes_depth(parent_of: Mapping[str, str | None]) -> int:
    if not parent_of:
        return 0
    return max(decomposes_depth(parent_of, qid) for qid in parent_of)


def question_origin_from_plans(
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> dict[str, str]:
    """question_id → origin (explicit|implicit|emergent); first wins."""
    found: dict[str, str] = {}
    rows = plans.values() if isinstance(plans, Mapping) else plans
    for plan in rows:
        if not isinstance(plan, Mapping):
            continue
        plan_origin = str(plan.get("origin") or "")
        for q in plan.get("questions") or []:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id") or "")
            if not qid or qid in found:
                continue
            raw = q.get("origin")
            if raw in VALID_DECOMPOSE_ORIGINS:
                found[qid] = str(raw)
            elif plan_origin == "emergent":
                found[qid] = "emergent"
            elif q.get("parent_id"):
                found[qid] = "explicit"
            else:
                found[qid] = "explicit"
        for q in plan.get("subquestions") or []:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id") or "")
            if not qid or qid in found:
                continue
            raw = q.get("origin")
            found[qid] = str(raw) if raw in VALID_DECOMPOSE_ORIGINS else "explicit"
        for q in plan.get("added_questions") or []:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id") or "")
            if not qid or qid in found:
                continue
            raw = q.get("origin")
            if raw in VALID_DECOMPOSE_ORIGINS:
                found[qid] = str(raw)
            elif plan_origin == "emergent":
                found[qid] = "emergent"
            else:
                found[qid] = "explicit"
    return found


def question_revision_from_plans(
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> dict[str, int]:
    """question_id → plan revision where it first appeared."""
    found: dict[str, int] = {}
    rows = list(plans.values() if isinstance(plans, Mapping) else plans)

    def _rev(plan: Mapping[str, Any]) -> int:
        for key in ("revision", "plan_rev"):
            v = plan.get(key)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        return 1

    for plan in sorted(rows, key=_rev):
        if not isinstance(plan, Mapping):
            continue
        rev = _rev(plan)
        for bucket in ("questions", "subquestions", "added_questions"):
            for q in plan.get(bucket) or []:
                if not isinstance(q, dict):
                    continue
                qid = str(q.get("id") or "")
                if qid and qid not in found:
                    found[qid] = rev
    return found


def plan_rollup(
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> RollupRule:
    """Latest non-empty rollup from plan revisions (default ``all``)."""
    rows = plans.values() if isinstance(plans, Mapping) else plans
    rule: str = "all"
    best_rev = -1
    for plan in rows:
        if not isinstance(plan, Mapping):
            continue
        raw = plan.get("rollup")
        if raw not in VALID_ROLLUP_RULES:
            continue
        rev = plan.get("revision")
        try:
            r = int(rev) if rev is not None else 0
        except (TypeError, ValueError):
            r = 0
        if r >= best_rev:
            best_rev = r
            rule = str(raw)
    return rule  # type: ignore[return-value]


def superseded_task_keys_from_plans(
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> set[str]:
    keys: set[str] = set()
    rows = plans.values() if isinstance(plans, Mapping) else plans
    for plan in rows:
        if not isinstance(plan, Mapping):
            continue
        for k in plan.get("superseded_tasks") or []:
            keys.add(str(k))
    return keys


def majority_threshold(n: int) -> int:
    """Strict majority: ⌈n/2⌉."""
    if n <= 0:
        return 0
    return int(ceil(n / 2))
