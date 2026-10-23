"""Rubric helpers (M1 PR13 / DESIGN_M1 §6): digest, aggregate, final_type projection.

Qualitative deliverables use ``final_type=artifact`` + frozen ``Rubric`` on
``OutputSpec``. Rubric results affect task outcome and artifact-question
acceptance only — never claim stance.
"""

from __future__ import annotations

from typing import Any

from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.domain import ReqStatus
from coscientist.core.rubric_types import (
    VALID_FINAL_TYPES,
    FinalType,
    Rubric,
    RubricItem,
)

__all__ = [
    "FinalType",
    "VALID_FINAL_TYPES",
    "Rubric",
    "RubricItem",
    "rubric_digest",
    "freeze_rubric",
    "aggregate_rubric_status",
    "question_final_types",
    "task_rubrics_met",
    "freeze_task_rubrics",
]


def rubric_digest(items: list[RubricItem], aggregate: str, k: int | None) -> str:
    """sha256(JCS(items+aggregate[+k])) — design §6.2."""
    payload: dict[str, Any] = {
        "items": [it.model_dump(mode="json") for it in items],
        "aggregate": aggregate,
    }
    if aggregate == "k_of_n":
        payload["k"] = k
    return sha256_hex(canonical_json(payload))


def freeze_rubric(rubric: Rubric) -> Rubric:
    """Return rubric with digest filled (idempotent if digest already matches)."""
    dig = rubric_digest(list(rubric.items), rubric.aggregate, rubric.k)
    if rubric.digest and rubric.digest != dig:
        raise ValueError(
            f"rubric digest mismatch: provided {rubric.digest!r} != computed {dig!r}"
        )
    if rubric.digest == dig:
        return rubric
    return rubric.model_copy(update={"digest": dig})


def freeze_task_rubrics(spec: Any) -> Any:
    """Freeze every OutputSpec.rubric digest on a TaskSpec (pure)."""
    outs = list(getattr(spec, "expected_outputs", None) or [])
    if not outs:
        return spec
    new_outs = []
    changed = False
    for o in outs:
        rub = getattr(o, "rubric", None)
        if rub is None:
            new_outs.append(o)
            continue
        frozen = freeze_rubric(rub if isinstance(rub, Rubric) else Rubric.model_validate(rub))
        if frozen is not rub:
            changed = True
            new_outs.append(o.model_copy(update={"rubric": frozen}))
        else:
            new_outs.append(o)
    if not changed:
        return spec
    return spec.model_copy(update={"expected_outputs": new_outs})


def aggregate_rubric_status(
    item_statuses: dict[str, ReqStatus],
    rubric: Rubric,
) -> ReqStatus:
    """Combine per-item statuses → overall rubric status.

    Required items must all be MET (or WAIVED/NOT_APPLICABLE). Optional items
    never block MET. Any PENDING → PENDING. Judge failure → UNASSESSED (R23),
    not FAILED. Code FAILED on a required item → FAILED.
    """
    required = [it for it in rubric.items if it.required]
    if any(
        item_statuses.get(it.id, ReqStatus.PENDING) == ReqStatus.PENDING
        for it in rubric.items
    ):
        return ReqStatus.PENDING

    def _ok(st: ReqStatus) -> bool:
        return st in {ReqStatus.MET, ReqStatus.WAIVED, ReqStatus.NOT_APPLICABLE}

    unassessed_required = [
        it
        for it in required
        if item_statuses.get(it.id) == ReqStatus.UNASSESSED and it.check == "judge"
    ]
    failed_required = [
        it
        for it in required
        if item_statuses.get(it.id)
        in {ReqStatus.FAILED, ReqStatus.MIGRATION_ERROR}
    ]
    if failed_required:
        return ReqStatus.FAILED
    if unassessed_required:
        return ReqStatus.UNASSESSED

    if rubric.aggregate == "all_required":
        if required and all(
            _ok(item_statuses.get(it.id, ReqStatus.PENDING)) for it in required
        ):
            return ReqStatus.MET
        if not required:
            opts = [
                item_statuses.get(it.id, ReqStatus.PENDING) for it in rubric.items
            ]
            if opts and all(_ok(s) for s in opts):
                return ReqStatus.MET
            return ReqStatus.UNASSESSED
        return ReqStatus.FAILED

    met_n = sum(
        1
        for it in rubric.items
        if _ok(item_statuses.get(it.id, ReqStatus.PENDING))
    )
    k = int(rubric.k or 0)
    if met_n >= k:
        return ReqStatus.MET
    if any(
        item_statuses.get(it.id) == ReqStatus.UNASSESSED and it.check == "judge"
        for it in rubric.items
    ):
        return ReqStatus.UNASSESSED
    return ReqStatus.FAILED


def question_final_types(plans: Any) -> dict[str, FinalType]:
    """question_id → final_type across PlanAccepted revisions (first wins)."""
    found: dict[str, FinalType] = {}
    rows = (
        plans.values()
        if hasattr(plans, "values") and not isinstance(plans, list)
        else plans
    )
    for plan in rows or []:
        if not isinstance(plan, dict):
            continue
        for q in plan.get("questions") or []:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id", ""))
            if not qid or qid in found:
                continue
            ft = str(q.get("final_type") or "verdict")
            if ft not in VALID_FINAL_TYPES:
                ft = "verdict"
            found[qid] = ft  # type: ignore[assignment]
    return found


def task_rubrics_met(
    *,
    task_keys: set[str],
    tasks: Any,
    results: Any | None,
) -> bool:
    """True iff ≥1 covering DONE SUCCESS/PARTIAL task has OutputSpec rubrics MET."""
    try:
        by_key: dict[str, Any] = {}
        for t in (tasks.values() if hasattr(tasks, "values") else tasks) or []:
            spec = getattr(t, "spec", None)
            if spec is None:
                continue
            by_key[str(spec.task_key)] = t
        res_map = results or {}
        for key in sorted(task_keys):
            t = by_key.get(key)
            if t is None:
                continue
            outcome = getattr(t, "outcome", None)
            if outcome is None:
                continue
            ov = getattr(outcome, "value", outcome)
            if ov not in ("SUCCESS", "PARTIAL"):
                continue
            rubrics = [
                o.rubric
                for o in (t.spec.expected_outputs or [])
                if getattr(o, "rubric", None) is not None
            ]
            if not rubrics:
                continue
            if _rubric_checks_met_for_task(t, res_map, rubrics):
                return True
        return False
    except Exception:  # noqa: BLE001 — dispositions/finalize totality
        return False


def _rubric_checks_met_for_task(task: Any, results: Any, rubrics: list[Any]) -> bool:
    digests = {
        str(getattr(r, "digest", None) or "")
        for r in rubrics
        if getattr(r, "digest", None)
    }
    attempt_ids = list(getattr(task, "attempts", None) or [])
    for aid in reversed(attempt_ids):
        row = _result_row(results, aid)
        if not isinstance(row, dict):
            continue
        crs = row.get("check_results") or []
        met_digests: set[str] = set()
        any_rubric_met = False
        for cr in crs:
            if not isinstance(cr, dict):
                continue
            st = cr.get("status")
            if st not in (ReqStatus.MET.value, ReqStatus.MET):
                continue
            tid = str(cr.get("template_id") or "")
            detail = str(cr.get("detail") or "")
            if tid.startswith("rubric:") or tid == "rubric_check" or "rubric" in tid:
                any_rubric_met = True
            for d in digests:
                if d and (d in tid or d in detail or f"rubric:{d}" == tid):
                    met_digests.add(d)
        if digests and digests <= met_digests:
            return True
        if not digests and any_rubric_met:
            return True
    return False


def _result_row(results: Any, aid: str) -> dict[str, Any] | None:
    if not isinstance(results, dict):
        return None
    row = results.get(aid) or results.get(f"{aid}:result")
    if isinstance(row, dict):
        return row
    for v in results.values():
        if isinstance(v, dict) and v.get("attempt_id") == aid:
            return v
    return None
