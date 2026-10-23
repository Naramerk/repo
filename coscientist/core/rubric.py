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

# Digest schema version — bump when canonical fields change.
RUBRIC_DIGEST_VERSION = 1

RUBRIC_REJECTED_REASON = "rubric_rejected"
RUBRIC_REJECTED_LINE = "не выполнено: рубрика не принята"

__all__ = [
    "FinalType",
    "VALID_FINAL_TYPES",
    "Rubric",
    "RubricItem",
    "RUBRIC_DIGEST_VERSION",
    "RUBRIC_REJECTED_REASON",
    "RUBRIC_REJECTED_LINE",
    "rubric_digest",
    "freeze_rubric",
    "aggregate_rubric_status",
    "question_final_types",
    "task_rubrics_met",
    "task_rubric_status",
    "freeze_task_rubrics",
]


def rubric_digest(items: list[RubricItem], aggregate: str, k: int | None) -> str:
    """sha256(JCS(canonical rubric fields)) — stable across model_dump churn."""
    canon_items = [
        {
            "id": it.id,
            "text": it.text,
            "required": bool(it.required),
            "check": it.check,
            "code_kind": it.code_kind,
        }
        for it in items
    ]
    payload: dict[str, Any] = {
        "v": RUBRIC_DIGEST_VERSION,
        "items": canon_items,
        "aggregate": aggregate,
        "k": k if aggregate == "k_of_n" else None,
    }
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
        frozen = freeze_rubric(
            rub if isinstance(rub, Rubric) else Rubric.model_validate(rub)
        )
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

    Required items drive MET/FAILED. Optional items only affect report score.
    Any PENDING → PENDING. Judge infra failure → UNASSESSED (R23).
    Code/judge not-met (FAILED) on a required item → FAILED.
    ``k_of_n`` counts MET among **required** items only.
    """
    required = [it for it in rubric.items if it.required]
    pool = required if required else list(rubric.items)
    if any(
        item_statuses.get(it.id, ReqStatus.PENDING) == ReqStatus.PENDING
        for it in pool
    ):
        return ReqStatus.PENDING

    def _ok(st: ReqStatus) -> bool:
        return st in {ReqStatus.MET, ReqStatus.WAIVED, ReqStatus.NOT_APPLICABLE}

    unassessed_required = [
        it
        for it in required
        if item_statuses.get(it.id) == ReqStatus.UNASSESSED
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

    # k_of_n: count only required items (optional never toward k).
    met_n = sum(
        1 for it in required if _ok(item_statuses.get(it.id, ReqStatus.PENDING))
    )
    k = int(rubric.k or 0)
    if met_n >= k:
        return ReqStatus.MET
    if any(
        item_statuses.get(it.id) == ReqStatus.UNASSESSED for it in required
    ):
        return ReqStatus.UNASSESSED
    return ReqStatus.FAILED


def question_final_types(plans: Any) -> dict[str, FinalType]:
    """question_id → final_type across PlanAccepted revisions (first wins).

    Unknown ``final_type`` values are skipped (caller/plan accept must refuse).
    """
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
            raw = q.get("final_type")
            if raw is None or raw == "":
                found[qid] = "verdict"
                continue
            ft = str(raw)
            if ft not in VALID_FINAL_TYPES:
                continue  # unknown — not silently coerced
            found[qid] = ft  # type: ignore[assignment]
    return found


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


def _is_rubric_check(cr: dict[str, Any], digests: set[str]) -> str | None:
    """Return matching digest if this CheckResult is a rubric check for one of digests."""
    raw_effect = cr.get("effect")
    effect: dict[str, Any] = raw_effect if isinstance(raw_effect, dict) else {}
    dig = str(effect.get("rubric_digest") or "")
    if dig and dig in digests:
        return dig
    tid = str(cr.get("template_id") or "")
    cid = str(cr.get("check_id") or "")
    for d in digests:
        if tid == f"rubric:{d}" or cid.startswith(f"rubric:{d}"):
            return d
        if effect.get("rubric_kind") == "rubric" and d and d in tid:
            return d
    return None


def task_rubric_status(
    *,
    task_keys: set[str],
    tasks: Any,
    results: Any | None,
) -> str | None:
    """Best rubric status across covering tasks: ``met`` | ``failed`` | ``pending`` | None.

    ``None`` = no rubric on covering tasks. Errors propagate (no silent swallow).
    """
    by_key: dict[str, Any] = {}
    for t in (tasks.values() if hasattr(tasks, "values") else tasks) or []:
        spec = getattr(t, "spec", None)
        if spec is None:
            continue
        by_key[str(spec.task_key)] = t
    res_map = results or {}
    seen_failed = False
    seen_pending = False
    for key in sorted(task_keys):
        t = by_key.get(key)
        if t is None:
            continue
        rubrics = [
            o.rubric
            for o in (t.spec.expected_outputs or [])
            if getattr(o, "rubric", None) is not None
        ]
        if not rubrics:
            continue
        digests = {
            str(getattr(r, "digest", None) or "")
            for r in rubrics
            if getattr(r, "digest", None)
        }
        attempt_ids = list(getattr(t, "attempts", None) or [])
        for aid in reversed(attempt_ids):
            row = _result_row(res_map, aid)
            if not isinstance(row, dict):
                continue
            for cr in row.get("check_results") or []:
                if not isinstance(cr, dict):
                    continue
                matched = _is_rubric_check(cr, digests) if digests else None
                if matched is None and digests:
                    continue
                if not digests and not (
                    isinstance(cr.get("effect"), dict)
                    and cr["effect"].get("rubric_kind") == "rubric"
                ):
                    continue
                st = cr.get("status")
                if st in (ReqStatus.MET.value, ReqStatus.MET):
                    return "met"
                if st in (ReqStatus.FAILED.value, ReqStatus.FAILED):
                    seen_failed = True
                if st in (ReqStatus.PENDING.value, ReqStatus.PENDING, ReqStatus.UNASSESSED.value, ReqStatus.UNASSESSED):
                    seen_pending = True
            # Also infer from task outcome when checks present but not met.
            outcome = getattr(t, "outcome", None)
            ov = getattr(outcome, "value", outcome) if outcome is not None else None
            if ov == "FAILED":
                seen_failed = True
    if seen_failed:
        return "failed"
    if seen_pending:
        return "pending"
    return None


def task_rubrics_met(
    *,
    task_keys: set[str],
    tasks: Any,
    results: Any | None,
) -> bool:
    """True iff ≥1 covering task has OutputSpec rubrics MET (structured identity).

    Errors propagate — callers that need totality (dispositions) catch explicitly.
    """
    return task_rubric_status(task_keys=task_keys, tasks=tasks, results=results) == "met"
