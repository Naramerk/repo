"""Pure projection: question_id → covering task_keys (PlanAccepted addresses)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any, get_args

from coscientist.core.execution.domain import AcceptanceRoute


def question_tasks(
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> dict[str, set[str]]:
    """Union TaskSpec.addresses across all accepted plan revisions.

    Tasks with empty addresses belong to the study (not returned here).
    """
    out: dict[str, set[str]] = defaultdict(set)
    rows = plans.values() if isinstance(plans, Mapping) else plans
    for plan in rows:
        for spec in plan.get("tasks") or []:
            if not isinstance(spec, dict):
                continue
            key = str(spec.get("task_key", ""))
            for qid in spec.get("addresses") or []:
                out[str(qid)].add(key)
    return dict(out)


def known_questions(
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> dict[str, str]:
    """question_id → text from Plan.questions across revisions (first text wins)."""
    found: dict[str, str] = {}
    rows = plans.values() if isinstance(plans, Mapping) else plans
    for plan in rows:
        for q in plan.get("questions") or []:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id", ""))
            if qid and qid not in found:
                found[qid] = str(q.get("text", ""))
    return found


def question_acceptance_routes(
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> dict[str, str]:
    """question_id → acceptance_route from Plan.questions (first revision wins).

    Same first-wins convention as ``question_final_types``. Absent / unknown
    route values are skipped — legacy frozen plans (universal-acceptance §8.v)
    keep the strict pre-S1 behavior, never silently re-routed.
    """
    known = frozenset(get_args(AcceptanceRoute))
    found: dict[str, str] = {}
    rows = plans.values() if isinstance(plans, Mapping) else plans
    for plan in rows:
        for q in plan.get("questions") or []:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id", ""))
            if not qid or qid in found:
                continue
            raw = q.get("acceptance_route")
            if raw is None or raw == "":
                continue
            route = str(raw)
            if route in known:
                found[qid] = route
    return found


def questions_from_spec_output(spec_out: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Extract QuestionSpec dicts from a ResearchSpecOut-shaped payload.

    Orchestrators call ``accept_plan(..., questions=[QuestionSpec(**q) for q in ...])``.
    Accepts ``questions`` as list of ``{id, text}`` or ``{formulation}`` (id derived).
    Optional ``final_type`` (PR13) is passed through when present.
    """
    if not spec_out:
        return []
    raw = spec_out.get("questions") or []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            out.append({"id": f"Q{i + 1}", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or item.get("question_id") or f"Q{i + 1}")
        text = str(item.get("text") or item.get("formulation") or "")
        row: dict[str, Any] = {"id": qid, "text": text}
        ft = item.get("final_type")
        if ft is not None and ft != "":
            # Pass through; PlanValidator / QuestionSpec refuse unknowns honestly.
            row["final_type"] = ft
        parent = item.get("parent_id")
        if parent:
            row["parent_id"] = str(parent)
        origin = item.get("origin")
        if origin is not None and origin != "":
            row["origin"] = origin
        out.append(row)
    return out
