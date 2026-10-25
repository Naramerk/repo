"""Shared ``kind=study`` GraphNode attrs (create + fork)."""

from __future__ import annotations

from coscientist.core.execution.domain import Study


def study_kind_attrs(
    study: Study, *, parent_study_id: str | None = None
) -> dict[str, object]:
    """Canonical study-node attrs — one shape for create and fork writers."""
    attrs: dict[str, object] = {
        "request": study.request,
        "goal": {"statement": study.request},
        "profile": study.profile,
        "catalog_version": str(study.catalog_version),
    }
    if parent_study_id is not None:
        attrs["parent_study_id"] = parent_study_id
    return attrs
