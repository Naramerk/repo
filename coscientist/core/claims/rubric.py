"""Rubric helpers (M1 PR13 / DESIGN_M1 §6): digest, aggregate, final_type projection.

Qualitative deliverables use ``final_type=artifact`` + frozen ``Rubric`` on
``OutputSpec``. Rubric results affect task outcome and artifact-question
acceptance only — never claim stance.
"""

from __future__ import annotations

from typing import Any

from coscientist.core.claims.rubric_types import (
    VALID_FINAL_TYPES,
    FinalType,
    Rubric,
    RubricItem,
)
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.execution.domain import ReqStatus

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
        raise ValueError(f"rubric digest mismatch: provided {rubric.digest!r} != computed {dig!r}")
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

    Required items drive MET/FAILED. Optional items only affect report score.
    Any PENDING → PENDING. Judge infra failure → UNASSESSED (R23).
    Code/judge not-met (FAILED) on a required item → FAILED.
    ``k_of_n`` counts MET among **required** items only.
    """
    required = [it for it in rubric.items if it.required]
    pool = required if required else list(rubric.items)
    if any(item_statuses.get(it.id, ReqStatus.PENDING) == ReqStatus.PENDING for it in pool):
        return ReqStatus.PENDING

    def _ok(st: ReqStatus) -> bool:
        return st in {ReqStatus.MET, ReqStatus.WAIVED, ReqStatus.NOT_APPLICABLE}

    unassessed_required = [it for it in required if item_statuses.get(it.id) == ReqStatus.UNASSESSED]
    failed_required = [
        it for it in required if item_statuses.get(it.id) in {ReqStatus.FAILED, ReqStatus.MIGRATION_ERROR}
    ]
    if failed_required:
        return ReqStatus.FAILED
    if unassessed_required:
        return ReqStatus.UNASSESSED

    if rubric.aggregate == "all_required":
        if required and all(_ok(item_statuses.get(it.id, ReqStatus.PENDING)) for it in required):
            return ReqStatus.MET
        if not required:
            opts = [item_statuses.get(it.id, ReqStatus.PENDING) for it in rubric.items]
            if opts and all(_ok(s) for s in opts):
                return ReqStatus.MET
            return ReqStatus.UNASSESSED
        return ReqStatus.FAILED

    # k_of_n: count only required items (optional never toward k).
    met_n = sum(1 for it in required if _ok(item_statuses.get(it.id, ReqStatus.PENDING)))
    k = int(rubric.k or 0)
    if met_n >= k:
        return ReqStatus.MET
    if any(item_statuses.get(it.id) == ReqStatus.UNASSESSED for it in required):
        return ReqStatus.UNASSESSED
    return ReqStatus.FAILED


def question_final_types(plans: Any) -> dict[str, FinalType]:
    """question_id → final_type across PlanAccepted revisions (first wins).

    Unknown ``final_type`` values are skipped (caller/plan accept must refuse).
    """
    found: dict[str, FinalType] = {}
    rows = plans.values() if hasattr(plans, "values") and not isinstance(plans, list) else plans
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
    """Return matching digest via structured identity (never substring match)."""
    raw_effect = cr.get("effect")
    effect: dict[str, Any] = raw_effect if isinstance(raw_effect, dict) else {}
    dig = str(effect.get("rubric_digest") or "")
    if dig and dig in digests:
        return dig
    tid = str(cr.get("template_id") or "")
    cid = str(cr.get("check_id") or "")
    for d in digests:
        if not d:
            continue
        if tid == f"rubric:{d}":
            return d
        # Structured check_id forms: ``rubric:<digest>#out`` / ``rubric:<digest>#0``.
        if cid == f"rubric:{d}#out" or cid.startswith(f"rubric:{d}#"):
            return d
    if effect.get("rubric_kind") == "rubric" and dig and dig in digests:
        return dig
    return None


def _row_is_timeout_or_infra(row: dict[str, Any], cr: dict[str, Any]) -> bool:
    """Timeout/infra failures are UNASSESSED for rubric acceptance, not not-met."""
    rc = str(row.get("result_class") or "")
    if rc in {"timeout", "infra", "core_bug", "sandbox_infra"}:
        return True
    if cr.get("timeout_kind"):
        return True
    detail = str(cr.get("detail") or "")
    if detail.startswith("sandbox_") or "timeout" in detail.lower():
        reason = str(row.get("reason") or "")
        if reason in {
            "sandbox_infra",
            "sandbox_timeout",
            "idle_timeout",
            "catalog_error",
            "core_bug",
        }:
            return True
    return False


def task_rubric_status(
    *,
    task_keys: set[str],
    tasks: Any,
    results: Any | None,
) -> str | None:
    """Acceptance of all required covering outputs and validity checks.

    Evaluates only each task's **current** (latest) attempt. Timeout/infra FAILED
    counts as UNASSESSED (pending), not rubric rejection. Errors propagate.

    A MET on one covering task never covers another task's rubric — each
    ``task_key`` is evaluated independently against its own attempt results.
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
    seen_required = False
    for key in sorted(task_keys):
        t = by_key.get(key)
        if t is None:
            seen_pending = True
            continue
        if not t.spec.required:
            continue
        seen_required = True
        if str(t.state) != "DONE" or str(t.outcome) == "SKIPPED":
            seen_pending = True
            continue
        rubrics = [
            o.rubric for o in (t.spec.expected_outputs or []) if o.required and getattr(o, "rubric", None) is not None
        ]
        if not rubrics:
            seen_pending = True
            continue
        digests = {str(getattr(r, "digest", None) or "") for r in rubrics if getattr(r, "digest", None)}
        attempt_ids = list(getattr(t, "attempts", None) or [])
        if not attempt_ids:
            seen_pending = True
            continue
        # Current attempt only (latest).
        aid = attempt_ids[-1]
        row = _result_row(res_map, aid)
        if not isinstance(row, dict):
            seen_pending = True
            continue
        if str(row.get("result_class") or "") in {
            "timeout",
            "infra",
            "core_bug",
            "sandbox_infra",
        }:
            seen_pending = True
            continue
        found_digests: set[str] = set()
        criterion_ids = {c.template_id for c in t.spec.criteria if c.required}
        found_criteria: set[str] = set()
        for cr in row.get("check_results") or []:
            if not isinstance(cr, dict):
                continue
            tid = str(cr.get("template_id") or "")
            if tid in criterion_ids:
                found_criteria.add(tid)
            if cr.get("required") and cr.get("role") == "validity":
                if cr.get("status") in (ReqStatus.FAILED.value, ReqStatus.MIGRATION_ERROR.value):
                    seen_failed = True
                elif cr.get("status") != ReqStatus.MET.value:
                    seen_pending = True
            matched = _is_rubric_check(cr, digests) if digests else None
            if matched is None and digests:
                continue
            if not digests and not (isinstance(cr.get("effect"), dict) and cr["effect"].get("rubric_kind") == "rubric"):
                continue
            if matched is not None:
                found_digests.add(matched)
            st = cr.get("status")
            if st not in (ReqStatus.MET.value, ReqStatus.FAILED.value):
                seen_pending = True
            if st in (ReqStatus.FAILED.value, ReqStatus.FAILED):
                if _row_is_timeout_or_infra(row, cr):
                    seen_pending = True
                else:
                    seen_failed = True
            if st in (
                ReqStatus.PENDING.value,
                ReqStatus.PENDING,
                ReqStatus.UNASSESSED.value,
                ReqStatus.UNASSESSED,
            ):
                seen_pending = True
        if found_digests != digests or found_criteria != criterion_ids:
            seen_pending = True
    if seen_failed:
        return "failed"
    if seen_pending:
        return "pending"
    if seen_required:
        return "met"
    return None


def task_rubrics_met(
    *,
    task_keys: set[str],
    tasks: Any,
    results: Any | None,
) -> bool:
    """True iff every required covering output/check is accepted.

    Errors propagate — callers that need totality (dispositions) catch explicitly.
    """
    return task_rubric_status(task_keys=task_keys, tasks=tasks, results=results) == "met"
