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

# Preference order for ``any`` when multiple *child* resolved dispositions differ.
# Never used to raise a parent above its own PR 8b claims-based verdict (see
# ``_combine_with_own_claims``).
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

# Higher = more positive. Rollup may only keep or lower a claims-based parent.
_DISP_RANK: dict[str, int] = {
    Disposition.ANSWERED.value: 80,
    Disposition.SURVIVED.value: 70,
    Disposition.NEEDS_MORE_DATA.value: 55,
    Disposition.INCONCLUSIVE.value: 50,
    Disposition.RUBRIC_NOT_MET.value: 40,
    Disposition.ILL_POSED.value: 35,
    Disposition.INFEASIBLE.value: 30,
    Disposition.REFUTED.value: 20,
    Disposition.NOT_ATTEMPTED.value: 10,
}

# Dispositions that can come from counting claims (PR 8b / DESIGN_M1 §14).
_CLAIMS_DISPOSITIONS: frozenset[str] = frozenset(
    {
        Disposition.ANSWERED.value,
        Disposition.REFUTED.value,
        Disposition.SURVIVED.value,
        Disposition.INCONCLUSIVE.value,
    }
)


def apply_rollup(
    leaf_dispositions: dict[str, dict[str, Any]],
    *,
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Override parent rows using frozen ``Plan.rollup`` (leaves unchanged).

    ``rubric``: parents keep their own (artifact) disposition — no child override.
    Parents without children are unchanged.

    PR 8b / DESIGN_M1 §10+§14: when the parent already has a claims-based
    verdict, rollup may only **lower or keep** it — never raise (e.g.
    ``rollup=any`` must not turn a refuted parent into answered because a
    child is answered). Parents without counting claims (typical decomposition
    roots with ``not_attempted``) take the children rollup as today.
    """
    parent_of = parent_map_from_plans(plans)
    if not any(parent_of.values()):
        return leaf_dispositions
    kids = children_map(parent_of)
    if not kids:
        return leaf_dispositions
    rule = plan_rollup(plans)
    out = {qid: dict(row) for qid, row in leaf_dispositions.items()}
    depth_of: dict[str, int] = {}
    max_nodes = max(len(parent_of), 1) + 2

    def _depth(qid: str, stack: frozenset[str] = frozenset()) -> int:
        if qid in depth_of:
            return depth_of[qid]
        if qid in stack:
            raise ValueError(f"cyclic decomposes at {qid!r}")
        if len(stack) > max_nodes:
            raise ValueError("cyclic decomposes (depth guard)")
        parent = parent_of.get(qid)
        if not parent:
            d = 1
        else:
            d = _depth(parent, stack | {qid}) + 1
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
        own = out[pid]
        combined = _combine_with_own_claims(own, rolled)
        formulation = own.get("formulation", "")
        out[pid] = {
            **combined,
            "formulation": formulation,
            "rollup": rule,
            "rollup_children": list(kids[pid]),
            "final_type": own.get("final_type"),
        }
    return out


def _has_claims_based_verdict(row: Mapping[str, Any]) -> bool:
    """True when PR 8b counting claims already set a real disposition."""
    d = str(row.get("disposition") or "")
    if d not in _CLAIMS_DISPOSITIONS:
        return False
    # Claims / verdict path stamps ``verdict``; not_attempted fallbacks usually do not.
    return row.get("verdict") is not None


def _combine_with_own_claims(
    own: Mapping[str, Any], rolled: dict[str, Any]
) -> dict[str, Any]:
    """Clamp children rollup so it never raises above the parent's claims verdict."""
    if not _has_claims_based_verdict(own):
        return rolled
    own_d = str(own.get("disposition") or Disposition.NOT_ATTEMPTED.value)
    rolled_d = str(rolled.get("disposition") or Disposition.NOT_ATTEMPTED.value)
    if _DISP_RANK.get(rolled_d, 0) > _DISP_RANK.get(own_d, 0):
        detail = list(own.get("reason_detail") or [])
        detail.append("rollup_clamped_to_claims")
        out = dict(own)
        out["rollup_clamped"] = True
        out["rollup_would_have"] = rolled_d
        out["reason_detail"] = detail
        if out.get("reason") is None:
            out["reason"] = "rollup_clamped_to_claims"
        return out
    return rolled


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
    """Strict majority ``n//2+1``. Ties (1:1, 2:2, …) → no majority."""
    need = majority_threshold(n)
    counts = Counter(disps)
    # Under strict majority at most one disposition can meet the bar.
    winners = [d for d, c in counts.items() if c >= need]
    if len(winners) != 1:
        return {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "rollup_no_majority",
            "reason_detail": ["rollup_majority"],
        }
    return {
        "disposition": winners[0],
        "reason": "rollup_majority",
        "reason_detail": ["rollup_majority"],
    }
