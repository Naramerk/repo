"""Journaled Artifact.kind stamps (M1 PR15).

Agents must not import this module (import-linter). Core emits
``ArtifactRegistered(stamped=True)`` in the same append batch as
``ResultRecorded``; ``apply_kind_stamp`` mutates meta **only in fold** after a
successful append. Pre-commit mutation is forbidden.

``_stamp_kind`` is private (fold / apply_kind_stamp only).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from coscientist.core.events.errors import ArtifactRejected
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.execution.domain import Artifact, ArtifactKind

VALID_ARTIFACT_KINDS: frozenset[str] = frozenset({"experiment_design", "contradiction_map"})

__all__ = [
    "VALID_ARTIFACT_KINDS",
    "kind_stamped_event",
    "apply_kind_stamp",
]


def _stamp_kind(store: Any, artifact_id: str, kind: str | None) -> Artifact:
    """Apply ``kind`` to artifact meta (fold-internal only)."""
    if kind is not None and kind not in VALID_ARTIFACT_KINDS:
        raise ArtifactRejected(f"invalid artifact kind: {kind!r}")
    art = store.get_meta(artifact_id)
    if art is None:
        raise ArtifactRejected(f"artifact missing for kind stamp: {artifact_id!r}")
    if art.kind == kind:
        return art
    updated = art.model_copy(update={"kind": kind})
    store._index[artifact_id] = updated  # noqa: SLF001 — LocalArtifactStore
    (store.meta / f"{artifact_id}.json").write_text(
        updated.model_dump_json(), encoding="utf-8"
    )
    return updated


def apply_kind_stamp(store: Any, artifact_id: str, kind: str | None) -> Artifact:
    """Fold-time apply. Missing meta raises ``ArtifactRejected`` (caller may soften)."""
    if kind is not None and kind not in VALID_ARTIFACT_KINDS:
        raise ArtifactRejected(f"invalid artifact kind: {kind!r}")
    art = store.get_meta(artifact_id)
    if art is None:
        raise ArtifactRejected(f"artifact missing for kind stamp on replay: {artifact_id!r}")
    if art.kind == kind:
        return art
    return _stamp_kind(store, artifact_id, kind)


def kind_stamped_event(
    *,
    study_id: str,
    artifact_id: str,
    kind: ArtifactKind | Literal["experiment_design", "contradiction_map"],
    now: datetime,
    name: str | None = None,
    sha256: str | None = None,
    role: str | None = None,
) -> UnstampedEvent:
    """``ArtifactRegistered`` carrying core-stamped kind (idempotent per artifact)."""
    if kind not in VALID_ARTIFACT_KINDS:
        raise ArtifactRejected(f"invalid artifact kind: {kind!r}")
    payload: dict[str, Any] = {
        "artifact_id": artifact_id,
        "kind": kind,
        "name": name,
        "sha256": sha256,
        "role": role,
        "stamped": True,
    }
    return UnstampedEvent(
        study_id=study_id,
        type="ArtifactRegistered",
        entity_key=artifact_id,
        idem_key=make_idem("ArtifactRegistered", artifact_id, "kind", str(kind)),
        payload=payload,
        ts=now,
    )
