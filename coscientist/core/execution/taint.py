"""Taint from input artifacts (DESIGN_M1 §8 / K5) — computed in core, not by callers.

Per-attempt taint for sandbox/verifier puts uses a ``ContextVar`` so parallel
foreach attempts do not clobber each other (unlike shared store state).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar, Token
from typing import Any

from coscientist.core.finalize.soft_failures import FINALIZE_SOFT_FAILURES

GetMeta = Callable[[str], Any | None]

# Per-async-task / per-thread (copied into to_thread on 3.12+).
# Mutable cell so a mid-attempt fetch can flip taint without losing the
# executor's reset token (foreach-safe: each context has its own cell).
_attempt_context_tainted: ContextVar[list[bool] | None] = ContextVar(
    "attempt_context_tainted", default=None
)
def push_attempt_taint(value: bool) -> Token[list[bool] | None]:
    """Bind attempt taint for the current context; return token for reset."""
    return _attempt_context_tainted.set([bool(value)])


def reset_attempt_taint(token: Token[list[bool] | None]) -> None:
    _attempt_context_tainted.reset(token)


def mark_attempt_tainted() -> None:
    """Flip the current context cell to tainted (sticky until reset)."""
    cell = _attempt_context_tainted.get()
    if cell is None:
        _attempt_context_tainted.set([True])
    else:
        cell[0] = True


def current_attempt_taint() -> bool:
    cell = _attempt_context_tainted.get()
    if cell is None:
        return False
    return bool(cell[0])


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
        lookup_errors: list[BaseException] = []
        try:
            meta = get_meta(aid)
        except FINALIZE_SOFT_FAILURES as exc:
            lookup_errors.append(exc)
        if lookup_errors:
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
    return bool(context_tainted or explicit or current_attempt_taint())
