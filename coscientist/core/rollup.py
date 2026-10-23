"""Parent disposition rollup from child questions (M1 PR16 / DESIGN_M1 §10)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from coscientist.core.decomposes import (
    children_map,
    majority_threshold,
    parent_map_from_plans,
    plan_rollup,
)
from coscientist.core.reasons import Disposition

# Honest terminal dispositions that count as "resolved" for all/any/majority.
_RESOLVED: frozenset[str] = frozenset(
    {
        Disposition.ANSWERED.value,
        Disposition.REFUTED.value,
        Disposition.SURVIVED.value,
        Disposition.NEEDS_MORE_DATA.value,
        Disposition.INFEASIBLE.value,
        Disposition.ILL_POSED.value,
        Disposition.RUBRIC_NOT_MET.value,
        Disposition.INCONCLUSIVE.value,
    }
)

# Preference order for ``any`` when multiple resolved dispositions differ.
_ANY_PREF: tuple[str, ...] = (
    Disposition.ANSWERED.value,
    Disposition.SURVIVED.value,
    Disposition.REFUTED.value,
    Disposition.NEEDS_MORE_DATA.value,
    Disposition.INFEASIBLE.value,
    Disposition.ILL_POSED.value,
    Disposition.RUBRIC_NOT_MET.value,
    Disposition.INCONCLUSIVE.value,
)


def apply_rollup(
    leaf_dispositions: dict[str, dict[str, Any]],
    *,
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Override parent rows using frozen ``Plan.rollup`` (leaves unchanged).

    ``rubric``: parents keep their own (artifact) disposition — no child override.
    Parents without children are unchanged.
    """
    parent_of = parent_map_from_plans(plans)
    if not any(parent_of.values()):
        return leaf_dispositions
    kids = children_map(parent_of)
    if not kids:
        return leaf_dispositions
    rule = plan_rollup(plans)
    out = {qid: dict(row) for qid, row in leaf_dispositions.items()}
    # Bottom-up: deepest parents first so multi-level trees compose.
    depth_of: dict[str, int] = {}

    def _depth(qid: str) -> int:
        if qid in depth_of:
            return depth_of[qid]
        parent = parent_of.get(qid)
        d = 1 if not parent else _depth(parent) + 1
        depth_of[qid] = d
        return d

    parents_ordered = sorted(kids.keys(), key=_depth, reverse=True)
    for pid in parents_ordered:
        if pid not in out:
            continue
        if rule == "rubric":
            # Parent disposition from own rubric/claims path only.
            out[pid] = {**out[pid], "rollup": "rubric", "rollup_children": list(kids[pid])}
            continue
        child_rows = [out[c] for c in kids[pid] if c in out]
        if not child_rows:
            continue
        rolled = _rollup_children(child_rows, rule=rule)
        formulation = out[pid].get("formulation", "")
        out[pid] = {
            **rolled,
            "formulation": formulation,
            "rollup": rule,
            "rollup_children": list(kids[pid]),
            "final_type": out[pid].get("final_type"),
        }
    return out


def _rollup_children(child_rows: list[dict[str, Any]], *, rule: str) -> dict[str, Any]:
    disps = [str(r.get("disposition") or Disposition.NOT_ATTEMPTED.value) for r in child_rows]
    n = len(disps)
    if rule == "all":
        return _rollup_all(disps)
    if rule == "any":
        return _rollup_any(disps)
    if rule == "majority":
        return _rollup_majority(disps, n=n)
    # Unknown → fail closed to not_attempted (should not happen after validation).
    return {
        "disposition": Disposition.NOT_ATTEMPTED.value,
        "reason": "rollup_unknown_rule",
        "reason_detail": [rule],
    }


def _rollup_all(disps: list[str]) -> dict[str, Any]:
    if any(d == Disposition.NOT_ATTEMPTED.value for d in disps):
        return {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "rollup_incomplete",
            "reason_detail": ["rollup_all"],
        }
    if all(d == disps[0] for d in disps):
        return {
            "disposition": disps[0],
            "reason": "rollup_all",
            "reason_detail": ["rollup_all"],
        }
    return {
        "disposition": Disposition.INCONCLUSIVE.value,
        "reason": "rollup_mixed",
        "reason_detail": ["rollup_all"],
        "verdict": "inconclusive",
    }


def _rollup_any(disps: list[str]) -> dict[str, Any]:
    resolved = [d for d in disps if d in _RESOLVED]
    if not resolved:
        return {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "rollup_incomplete",
            "reason_detail": ["rollup_any"],
        }
    for pref in _ANY_PREF:
        if pref in resolved:
            return {
                "disposition": pref,
                "reason": "rollup_any",
                "reason_detail": ["rollup_any"],
            }
    return {
        "disposition": resolved[0],
        "reason": "rollup_any",
        "reason_detail": ["rollup_any"],
    }


def _rollup_majority(disps: list[str], *, n: int) -> dict[str, Any]:
    need = majority_threshold(n)
    counts = Counter(disps)
    # Prefer a resolved disposition that meets majority; else not_attempted.
    winners = [(d, c) for d, c in counts.items() if c >= need]
    if not winners:
        return {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "rollup_no_majority",
            "reason_detail": ["rollup_majority"],
        }
    # Tie-break by _ANY_PREF then lexical.
    winners.sort(key=lambda pair: (_ANY_PREF.index(pair[0]) if pair[0] in _ANY_PREF else 99, pair[0]))
    # Among those with max count first.
    max_c = max(c for _, c in winners)
    top = [d for d, c in winners if c == max_c]
    top.sort(key=lambda d: (_ANY_PREF.index(d) if d in _ANY_PREF else 99, d))
    chosen = top[0]
    return {
        "disposition": chosen,
        "reason": "rollup_majority",
        "reason_detail": ["rollup_majority"],
    }
