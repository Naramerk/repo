"""Experiment-design deliverable (M1 PR15 / DESIGN_M1 §6.4).

``needs_more_data`` requires an artifact ``role=report`` with explicit
``OutputSpec.kind=experiment_design`` and a rubric covering hypothesis /
variables / sample_size / success_criterion. Core stamps ``Artifact.kind`` from
``OutputSpec.kind`` (PlanValidator-enforced; never inferred from item ids).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from coscientist.core.rubric import freeze_rubric
from coscientist.core.rubric_types import Rubric, RubricItem

# Required rubric item ids (DESIGN_M1 §6.4).
EXPERIMENT_DESIGN_ITEM_IDS: frozenset[str] = frozenset(
    {
        "hypothesis",
        "variables",
        "sample_size",
        "success_criterion",
    }
)

ARTIFACT_KIND_EXPERIMENT_DESIGN = "experiment_design"
ARTIFACT_KIND_CONTRADICTION_MAP = "contradiction_map"

MISSING_DESIGN_REASON = "missing_experiment_design"
MISSING_DESIGN_LINE = "не выполнено: needs_more_data без дизайна эксперимента"

__all__ = [
    "ARTIFACT_KIND_CONTRADICTION_MAP",
    "ARTIFACT_KIND_EXPERIMENT_DESIGN",
    "EXPERIMENT_DESIGN_ITEM_IDS",
    "MISSING_DESIGN_LINE",
    "MISSING_DESIGN_REASON",
    "experiment_design_rubric",
    "is_experiment_design_rubric",
    "is_contradiction_map_rubric",
    "artifact_kind_from_output_spec",
    "design_artifact_ok",
]


def experiment_design_rubric(*, judge: bool = True) -> Rubric:
    """Canonical frozen rubric for an experiment-design report."""
    check = "judge" if judge else "code"
    items = [
        RubricItem(
            id="hypothesis",
            text="Hypothesis under test is stated",
            required=True,
            check=check,  # type: ignore[arg-type]
            code_kind="artifact_exists" if not judge else None,
        ),
        RubricItem(
            id="variables",
            text="Independent and dependent variables are named",
            required=True,
            check=check,  # type: ignore[arg-type]
            code_kind="artifact_exists" if not judge else None,
        ),
        RubricItem(
            id="sample_size",
            text="Sample size (or simulation budget) is specified",
            required=True,
            check=check,  # type: ignore[arg-type]
            code_kind="artifact_exists" if not judge else None,
        ),
        RubricItem(
            id="success_criterion",
            text="Success / decision criterion is specified",
            required=True,
            check=check,  # type: ignore[arg-type]
            code_kind="artifact_exists" if not judge else None,
        ),
    ]
    return freeze_rubric(Rubric(items=items, aggregate="all_required"))


def _as_rubric(rubric: Rubric | Any | None) -> Rubric | None:
    if rubric is None:
        return None
    if isinstance(rubric, Rubric):
        return rubric
    try:
        return Rubric.model_validate(rubric)
    except ValidationError:
        return None


def is_experiment_design_rubric(rubric: Rubric | Any | None) -> bool:
    """True iff required item ids cover the §6.4 set."""
    r = _as_rubric(rubric)
    if r is None:
        return False
    req_ids = {it.id for it in r.items if it.required}
    return EXPERIMENT_DESIGN_ITEM_IDS <= req_ids


def is_contradiction_map_rubric(rubric: Rubric | Any | None) -> bool:
    """True when rubric declares completeness + contradictions items (§6.5)."""
    r = _as_rubric(rubric)
    if r is None:
        return False
    ids = {it.id for it in r.items}
    return "completeness" in ids and "contradictions" in ids


def artifact_kind_from_output_spec(spec: Any | None) -> str | None:
    """Return explicit ``OutputSpec.kind`` only (never infer from rubric ids)."""
    if spec is None:
        return None
    kind = getattr(spec, "kind", None)
    if kind in (ARTIFACT_KIND_EXPERIMENT_DESIGN, ARTIFACT_KIND_CONTRADICTION_MAP):
        return str(kind)
    return None


def _artifact_produced_by_covering_task(
    *,
    artifact_id: str,
    tasks: Any,
    results: Any,
    task_keys: set[str],
) -> bool:
    """True when ``artifact_id`` is an output of a covering task with design kind."""
    by_tid: dict[str, Any] = {}
    for t in tasks.values() if hasattr(tasks, "values") else tasks:
        tid = getattr(t, "task_id", None)
        if tid is not None:
            by_tid[str(tid)] = t
    rows = results.values() if hasattr(results, "values") else results
    for res in rows:
        if not isinstance(res, dict):
            continue
        arts = list(res.get("artifacts") or [])
        omap = res.get("output_map") or {}
        if isinstance(omap, dict):
            arts.extend(str(v) for v in omap.values() if v)
        if str(artifact_id) not in {str(a) for a in arts}:
            continue
        tid = str(res.get("task_id") or "")
        task = by_tid.get(tid)
        if task is None:
            continue
        spec = getattr(task, "spec", None)
        if spec is None:
            continue
        tkey = str(getattr(spec, "task_key", "") or "")
        if tkey not in task_keys:
            continue
        for o in getattr(spec, "expected_outputs", None) or []:
            if artifact_kind_from_output_spec(o) == ARTIFACT_KIND_EXPERIMENT_DESIGN:
                return True
    return False


def design_artifact_ok(
    *,
    artifact_id: str | None,
    artifact_lookup: Any | None = None,
    tasks: Any | None = None,
    results: Any | None = None,
    task_keys: set[str] | None = None,
    journal_kinds: dict[str, str] | None = None,
    pending_design_ids: set[str] | None = None,
) -> bool:
    """True when ``artifact_id`` is a journaled/pending experiment_design.

    Never trusts live ``meta.kind`` alone (pre-commit stamp mutation). Accepts:
    - ``pending_design_ids`` (same-batch OutputSpec.kind=experiment_design outputs)
    - ``journal_kinds`` from folded ``ArtifactRegistered(stamped=True)``
    - covering-task OutputSpec.kind binding via results (not rubric-id spoof)
    """
    del artifact_lookup  # meta.kind is not authoritative without journal stamp
    if not artifact_id:
        return False
    aid = str(artifact_id)
    if pending_design_ids and aid in pending_design_ids:
        return True
    if journal_kinds and journal_kinds.get(aid) == ARTIFACT_KIND_EXPERIMENT_DESIGN:
        return True
    if tasks is None or results is None or not task_keys:
        return False
    return _artifact_produced_by_covering_task(
        artifact_id=aid,
        tasks=tasks,
        results=results,
        task_keys=task_keys,
    )
