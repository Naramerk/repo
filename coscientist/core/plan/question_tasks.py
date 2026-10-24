"""Pure projection: question_id → covering task_keys (PlanAccepted addresses)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any


def question_tasks(
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> dict[str, set[str]]:
    """Union TaskSpec.addresses across all accepted plan revisions.

    Tasks with empty addresses belong to the study (not returned here).
    """
    out: dict[str, set[str]] = defaultdict(set)
    rows = plans.values() if isinstance(plans, Mapping) else plans  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
    for plan in rows:
        for spec in plan.get("tasks") or []:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # noqa: E501
            if not isinstance(spec, dict):
                continue
            key = str(spec.get("task_key", ""))  # audit-ok[coerce_str]: display/wire stringify after type gate  # audit-ok[dict_get_default]: explicit default only for structural absent key  # audit-ok[dict_get_default]: explicit default only for structural absent key  # noqa: E501
            for qid in spec.get("addresses") or []:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # noqa: E501
                out[str(qid)].add(key)
    return dict(out)


def known_questions(
    plans: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
) -> dict[str, str]:
    """question_id → text from Plan.questions across revisions (first text wins)."""
    found: dict[str, str] = {}  # audit-ok[field_default]: structural empty/collector; not invented domain value
    rows = plans.values() if isinstance(plans, Mapping) else plans  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
    for plan in rows:
        for q in plan.get("questions") or []:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # noqa: E501
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id", ""))  # audit-ok[coerce_str]: display/wire stringify after type gate  # audit-ok[dict_get_default]: explicit default only for structural absent key  # audit-ok[dict_get_default]: explicit default only for structural absent key  # noqa: E501
            if qid and qid not in found:
                found[qid] = str(q.get("text", ""))  # audit-ok[coerce_str]: display/wire stringify after type gate  # audit-ok[dict_get_default]: explicit default only for structural absent key  # audit-ok[dict_get_default]: explicit default only for structural absent key  # noqa: E501
    return found


def questions_from_spec_output(spec_out: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Extract QuestionSpec dicts from a ResearchSpecOut-shaped payload.

    Orchestrators call ``accept_plan(..., questions=[QuestionSpec(**q, final_type="verdict") for q in ...])``.
    Accepts ``questions`` as list of ``{id, text}`` or ``{formulation}`` (id derived).
    Optional ``final_type`` (PR13) is passed through when present.
    """
    if not spec_out:
        return []
    raw = spec_out.get("questions") or []  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # noqa: E501
    out: list[dict[str, Any]] = []  # audit-ok[field_default]: structural empty/collector; not invented domain value
    for i, item in enumerate(raw):
        if isinstance(item, str):
            out.append({"id": f"Q{i + 1}", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or item.get("question_id") or f"Q{i + 1}")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_call]: coalesce existing call result; no invented literal  # audit-ok[or_call]: coalesce existing call result; no invented literal  # noqa: E501
        text = str(item.get("text") or item.get("formulation") or "")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_call]: coalesce existing call result; no invented literal # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # audit-ok[or_call]: coalesce existing call result; no invented literal # audit-ok[or_literal]: structural empty coalesce; absent → empty container/str  # noqa: E501
        row: dict[str, Any] = {"id": qid, "text": text}
        ft = item.get("final_type")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
        if ft is not None and ft != "":
            # Pass through; PlanValidator / QuestionSpec refuse unknowns honestly.
            row["final_type"] = ft
        parent = item.get("parent_id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
        if parent:
            row["parent_id"] = str(parent)
        origin = item.get("origin")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
        if origin is not None and origin != "":
            row["origin"] = origin
        out.append(row)
    return out
