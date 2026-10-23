"""Experiment-design deliverable (M1 PR15 / DESIGN_M1 §6.4).

``needs_more_data`` requires an artifact ``role=report`` whose OutputSpec rubric
covers hypothesis / variables / sample_size / success_criterion (ids). Core
stamps ``Artifact.kind=experiment_design`` from that rubric signature — no
journal schema bump.
"""

from __future__ import annotations

from typing import Any

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


def is_experiment_design_rubric(rubric: Rubric | Any | None) -> bool:
    """True iff required item ids cover the §6.4 set."""
    if rubric is None:
        return False
    if not isinstance(rubric, Rubric):
        try:
            rubric = Rubric.model_validate(rubric)
        except Exception:  # noqa: BLE001 — fail-closed
            return False
    req_ids = {it.id for it in rubric.items if it.required}
    return EXPERIMENT_DESIGN_ITEM_IDS <= req_ids


def artifact_kind_from_output_spec(spec: Any | None) -> str | None:
    """Stamp kind from plan OutputSpec (role + rubric signature)."""
    if spec is None:
        return None
    role = getattr(spec, "role", None)
    rub = getattr(spec, "rubric", None)
    if role == "report" and is_experiment_design_rubric(rub):
        return ARTIFACT_KIND_EXPERIMENT_DESIGN
    # Contradiction map: report + rubric item id ``completeness`` (PR15 §6.5).
    if role == "report" and rub is not None:
        try:
            r = rub if isinstance(rub, Rubric) else Rubric.model_validate(rub)
        except Exception:  # noqa: BLE001
            return None
        ids = {it.id for it in r.items}
        if "completeness" in ids and "contradictions" in ids:
            return ARTIFACT_KIND_CONTRADICTION_MAP
    return None


def design_artifact_ok(
    *,
    artifact_id: str | None,
    artifact_lookup: Any | None,
    tasks: Any | None = None,
    results: Any | None = None,
    task_keys: set[str] | None = None,
) -> bool:
    """True when ``artifact_id`` is a report experiment_design (meta kind or rubric).

    Prefer stamped ``Artifact.kind``. Fallback: producing task rubric signature
    when meta lacks kind (legacy puts).
    """
    if not artifact_id:
        return False
    meta = None
    if artifact_lookup is not None:
        try:
            meta = artifact_lookup(str(artifact_id))
        except Exception:  # noqa: BLE001 — totality
            meta = None
    if meta is not None:
        kind = getattr(meta, "kind", None)
        role = getattr(meta, "role", None)
        if kind == ARTIFACT_KIND_EXPERIMENT_DESIGN and role in (None, "report"):
            return True
        if kind == ARTIFACT_KIND_EXPERIMENT_DESIGN:
            return True
    # Fallback via covering task rubric (when kind not stamped).
    if tasks is None or not task_keys:
        return False
    from coscientist.core.rubric import task_rubric_status

    st = task_rubric_status(task_keys=task_keys, tasks=tasks, results=results)
    if st != "met":
        return False
    # Confirm at least one covering task carries the design rubric.
    by_key = {}
    for t in (tasks.values() if hasattr(tasks, "values") else tasks):
        spec = getattr(t, "spec", None)
        if spec is None:
            continue
        by_key[str(getattr(spec, "task_key", "") or "")] = spec
    for k in task_keys:
        spec = by_key.get(k)
        if spec is None:
            continue
        for o in getattr(spec, "expected_outputs", None) or []:
            if artifact_kind_from_output_spec(o) == ARTIFACT_KIND_EXPERIMENT_DESIGN:
                return True
    return False
