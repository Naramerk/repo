"""Parent disposition rollup from child questions (M1 PR16 / DESIGN_M1 §10)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from coscientist.core.claims.reasons import Disposition, TaskReason
from coscientist.core.plan.decomposes import (
    children_map,
    majority_threshold,
    parent_map_from_plans,
    plan_rollup,
)

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
# Never used to raise a parent above its own terminal verdict (see
# ``_combine_with_own_verdict``).
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

# Higher = more positive. Rollup may only keep or lower a terminal parent.
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


def apply_rollup(
    leaf_dispositions: dict[str, dict[str, Any]],
    *,
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Override parent rows using frozen ``Plan.rollup`` (leaves unchanged).

    ``rubric``: parents keep their own (artifact) disposition — no child override.
    Parents without children are unchanged.

    DESIGN_M1 §10+§14 / PR 8b: when the parent already has a **terminal own
    disposition or verdict** (``disposition != not_attempted`` or
    ``verdict is not None`` — claims, rubric_not_met, PR11 ``iteration_cap``,
    …), rollup may only **lower or keep** it — never raise, and never wipe it
    with ``not_attempted`` from incomplete / no-majority / cycle children.

    Cycle in the decomposes tree: only the cyclic subtree is tagged
    ``rollup_cycle`` (or clamped when the node already has a terminal own);
    unrelated questions keep their leaf / rolled dispositions (never a
    study-wide ``not_attempted`` wipe via dispositions' broad except).
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
    cyclic: set[str] = set()
    max_nodes = max(len(parent_of), 1) + 2

    def _depth(qid: str, stack: frozenset[str] = frozenset()) -> int:
        if qid in depth_of:
            return depth_of[qid]
        if qid in stack or len(stack) > max_nodes:
            cyclic.update(stack | {qid})
            depth_of[qid] = 0
            return 0
        parent = parent_of.get(qid)
        if not parent:
            d = 1
        else:
            d = _depth(parent, stack | {qid}) + 1
            if parent in cyclic:
                cyclic.add(qid)
        depth_of[qid] = d
        return d

    # Populate depths / cyclic set for every known node before ordering.
    for qid in parent_of:
        _depth(qid)

    def _in_cyclic_subtree(pid: str) -> bool:
        if pid in cyclic:
            return True
        return any(c in cyclic for c in kids.get(pid, ()))

    parents_ordered = sorted(
        (p for p in kids if not _in_cyclic_subtree(p)),
        key=_depth,
        reverse=True,
    )
    for pid in kids:
        if not _in_cyclic_subtree(pid):
            continue
        if pid not in out:
            continue
        # Localize: only the cyclic parent row is degraded — leaves elsewhere intact.
        # Like rollup_incomplete: keep own terminal disposition/verdict (clamp).
        own = out[pid]
        if _has_own_terminal_verdict(own):
            kept = _clamp_keep(own, Disposition.NOT_ATTEMPTED.value)
            detail = list(kept.get("reason_detail") or [])
            if "rollup_cycle" not in detail:
                detail.append("rollup_cycle")
            out[pid] = {
                **kept,
                "reason_detail": detail,
                "rollup": rule,
                "rollup_children": list(kids[pid]),
            }
        else:
            out[pid] = {
                **own,
                "disposition": Disposition.NOT_ATTEMPTED.value,
                "reason": "rollup_cycle",
                "reason_detail": ["rollup_cycle"],
                "rollup": rule,
                "rollup_children": list(kids[pid]),
            }

    for pid in parents_ordered:
        if pid not in out:
            continue
        if rule == "rubric":
            out[pid] = {**out[pid], "rollup": "rubric", "rollup_children": list(kids[pid])}
            continue
        child_rows = [out[c] for c in kids[pid] if c in out]
        if not child_rows:
            continue
        rolled = _rollup_children(child_rows, rule=rule)
        own = out[pid]
        combined = _combine_with_own_verdict(own, rolled)
        formulation = own.get("formulation", "")
        out[pid] = {
            **combined,
            "formulation": formulation,
            "rollup": rule,
            "rollup_children": list(kids[pid]),
            "final_type": own.get("final_type"),
        }
    return out


def _has_own_terminal_verdict(row: Mapping[str, Any]) -> bool:
    """True when the parent already carries a terminal own disposition/verdict.

    ``terminal is True`` **or** ``disposition != not_attempted`` **or**
    ``verdict is not None``.

    Covers PR15 honest terminals (``terminal=True`` without claim-style
    ``verdict``), PR 8b claims (``verdict``), ``rubric_not_met``, and PR11
    ``iteration_cap`` rows that stamp ``disposition=inconclusive`` without a
    ``verdict`` key (``dispositions.py`` ~383-388).
    """
    if row.get("terminal") is True:
        return True
    disp = str(row.get("disposition") or Disposition.NOT_ATTEMPTED.value)
    if disp != Disposition.NOT_ATTEMPTED.value:
        return True
    return row.get("verdict") is not None


def _clamp_keep(own: Mapping[str, Any], would_have: str) -> dict[str, Any]:
    detail = list(own.get("reason_detail") or [])
    detail.append("rollup_clamped_to_own")
    out = dict(own)
    out["rollup_clamped"] = True
    out["rollup_would_have"] = would_have
    out["reason_detail"] = detail
    if out.get("reason") is None:
        out["reason"] = "rollup_clamped_to_own"
    return out


def _combine_with_own_verdict(
    own: Mapping[str, Any], rolled: dict[str, Any]
) -> dict[str, Any]:
    """Clamp children rollup so it never raises (or wipes) a terminal parent."""
    if not _has_own_terminal_verdict(own):
        return rolled
    own_d = str(own.get("disposition") or Disposition.NOT_ATTEMPTED.value)
    rolled_d = str(rolled.get("disposition") or Disposition.NOT_ATTEMPTED.value)
    # Incomplete / no-majority → not_attempted must not overwrite a terminal own.
    if rolled_d == Disposition.NOT_ATTEMPTED.value:
        return _clamp_keep(own, rolled_d)
    own_rank = _DISP_RANK.get(own_d, 0)
    rolled_rank = _DISP_RANK.get(rolled_d, 0)
    if rolled_rank > own_rank:
        return _clamp_keep(own, rolled_d)
    if rolled_rank < own_rank:
        # Children may lower disposition; keep iteration_cap visible in detail.
        out = dict(rolled)
        if str(own.get("reason") or "") == TaskReason.ITERATION_CAP.value:
            detail = list(out.get("reason_detail") or [])
            if TaskReason.ITERATION_CAP.value not in detail:
                detail.append(TaskReason.ITERATION_CAP.value)
            out["reason_detail"] = detail
        return out
    # Equal rank: keep own (reason intact) — do not invent rollup_clamped.
    return dict(own)


def _rollup_children(child_rows: list[dict[str, Any]], *, rule: str) -> dict[str, Any]:
    disps = [str(r.get("disposition") or Disposition.NOT_ATTEMPTED.value) for r in child_rows]
    n = len(disps)
    if rule == "all":
        return _rollup_all(disps)
    if rule == "any":
        return _rollup_any(disps)
    if rule == "majority":
        return _rollup_majority(disps, n=n)
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
