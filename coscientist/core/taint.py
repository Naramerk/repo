"""Taint from input artifacts (DESIGN_M1 §8 / K5) — computed in core, not by callers."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

GetMeta = Callable[[str], Any | None]


def artifact_is_tainted(meta: Any | None) -> bool:
    if meta is None:
        return False
    if isinstance(meta, Mapping):
        return bool(meta.get("tainted"))
    return bool(getattr(meta, "tainted", False))


def context_tainted_from_metas(metas: Iterable[Any | None]) -> bool:
    return any(artifact_is_tainted(m) for m in metas)


def context_tainted_from_ids(
    artifact_ids: Sequence[str],
    get_meta: GetMeta | None,
) -> bool:
    """True when any listed artifact is marked tainted in the store."""
    if not artifact_ids or get_meta is None:
        return False
    for aid in artifact_ids:
        if not aid:
            continue
        try:
            meta = get_meta(aid)
        except Exception:  # noqa: BLE001 — missing meta ≠ crash the attempt
            continue
        if artifact_is_tainted(meta):
            return True
    return False


def bindings_tainted(
    bindings: Sequence[Any],
    get_meta: GetMeta | None,
) -> bool:
    """Compute attempt-context taint from P2 ``InputBinding`` rows + artifact store."""
    ids: list[str] = []
    for b in bindings:
        if isinstance(b, Mapping):
            aid = str(b.get("artifact_id") or "")
        else:
            aid = str(getattr(b, "artifact_id", "") or "")
        if aid:
            ids.append(aid)
    return context_tainted_from_ids(ids, get_meta)


def derived_tainted(*, context_tainted: bool, explicit: bool = False) -> bool:
    """Propagate context taint onto a newly stored artifact."""
    return bool(context_tainted or explicit)
