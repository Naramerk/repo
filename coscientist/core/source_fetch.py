"""Journaled fetch provenance for fetch-tool outputs (M1 PR14 / K9).

``SourceFetched`` records that a CAS digest was produced by a registered
**fetch** tool (``RegisteredToolSpec.is_fetch``). It is **not** part of the
EffectIntended/EffectDone index. ``quote_in_source`` accepts a source only when
its digest appears here **for the evaluating attempt**.

Minting ``FetchCapability`` is **not** in this module — only
``coscientist.tools.effects`` may mint (secret stays private there). This
module verifies via a verifier registered at effects import time (fail-closed
until registered).

Network content stays ``tainted=True``; journal match ≠ content trusted.
``countable=True`` with ``trusted=False`` is intentional (DESIGN_M1 §6.3 /
PR 8b): presence in a journaled fetch is core-evaluated countable evidence;
the bytes remain untrusted.

Payload v1: ``{call_id, tool, digest, url}``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from coscientist.core.domain import Catalog, CodeCheck, RequirementRef, TaskSpec
from coscientist.core.errors import ArtifactRejected
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.writer import StudyWriter

VerifyFetchCap = Callable[..., "FetchCapability"]

# Installed by coscientist.tools.effects at import (mint secret never lives here).
_verify_impl: VerifyFetchCap | None = None


def register_fetch_capability_verifier(fn: VerifyFetchCap) -> None:
    """Called once from ``tools.effects`` — core never mints capabilities."""
    global _verify_impl
    if _verify_impl is not None and _verify_impl is not fn:
        raise RuntimeError("fetch capability verifier already registered")
    _verify_impl = fn


@dataclass(slots=True)
class FetchCapability:
    """Opaque capability for put_fetched / SourceFetched (minted only in effects).

    ``_spent`` is a mutable single-use box: after consume, further verifies fail.
    """

    _token: object
    tool: str
    call_id: str
    study_id: str
    attempt_id: str
    _spent: list[bool] = field(default_factory=list)


def verify_fetch_capability(
    capability: Any,
    *,
    tool: str | None = None,
    call_id: str | None = None,
    study_id: str | None = None,
    attempt_id: str | None = None,
    consume: bool = False,
) -> FetchCapability:
    """Raise ``ArtifactRejected`` unless capability was minted by effects and binds."""
    if _verify_impl is None:
        raise ArtifactRejected("fetch_capability_required")
    return _verify_impl(
        capability,
        tool=tool,
        call_id=call_id,
        study_id=study_id,
        attempt_id=attempt_id,
        consume=consume,
    )


def task_needs_source_fetch(catalog: Catalog, spec: TaskSpec) -> bool:
    """True when evaluating this task may run ``quote_in_source`` (avoid journal scan)."""
    by_id = {t.id: t for t in catalog.templates}
    refs = list(spec.criteria)
    if not refs:
        for tid in catalog.defaults.get("experiment", []):
            refs.append(RequirementRef(template_id=tid))
    for ref in refs:
        tmpl = by_id.get(ref.template_id)
        if tmpl is None:
            continue
        check = tmpl.check
        if isinstance(check, CodeCheck) and check.kind == "quote_in_source":
            return True
    return False


def _attempt_id_from_call_id(call_id: str) -> str:
    """``call_id`` is ``{attempt_id}:{call_seq}`` — split on the last colon."""
    if ":" not in call_id:
        return call_id
    return call_id.rsplit(":", 1)[0]


def source_fetched_payload_ok(payload: dict[str, Any]) -> bool:
    """True when SourceFetched payload is paired with a fetch ToolCall shape."""
    call_id = str(payload.get("call_id") or "")
    tool = str(payload.get("tool") or "")
    digest = str(payload.get("digest") or "")
    url = payload.get("url")
    if not call_id or ":" not in call_id:
        return False
    if not tool or not digest:
        return False
    if not isinstance(url, str):
        return False
    return True


@dataclass
class SourceFetchIndex:
    """Attempt-scoped digest sets (plus study-wide view for diagnostics)."""

    by_digest: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_attempt: dict[str, set[str]] = field(default_factory=dict)

    def note(self, payload: dict[str, Any]) -> None:
        if not source_fetched_payload_ok(payload):
            return
        digest = str(payload.get("digest") or "")
        call_id = str(payload.get("call_id") or "")
        attempt_id = _attempt_id_from_call_id(call_id)
        self.by_digest[digest] = dict(payload)
        self.by_attempt.setdefault(attempt_id, set()).add(digest)

    def has(self, digest: str) -> bool:
        return digest in self.by_digest

    def digests(self) -> frozenset[str]:
        """Study-wide digests (diagnostics only — checks use attempt scope)."""
        return frozenset(self.by_digest)

    def digests_for_attempt(self, attempt_id: str) -> frozenset[str]:
        if not attempt_id:
            return frozenset()
        return frozenset(self.by_attempt.get(attempt_id, ()))


def apply_source_fetch_events(
    idx: SourceFetchIndex, events: list[Any]
) -> int:
    """Fold valid SourceFetched events; return max seq seen."""
    max_seq = 0
    for ev in events:
        seq = int(getattr(ev, "seq", 0) or 0)
        if seq > max_seq:
            max_seq = seq
        if getattr(ev, "type", None) != "SourceFetched":
            continue
        p = getattr(ev, "payload", None) or {}
        if isinstance(p, dict):
            idx.note(p)
    return max_seq


def load_source_fetch_index(events: list[Any]) -> SourceFetchIndex:
    idx = SourceFetchIndex()
    apply_source_fetch_events(idx, events)
    return idx


async def load_source_fetch_index_from_writer(writer: StudyWriter) -> SourceFetchIndex:
    """Incremental fold cached on the writer (O(Δ) after first load)."""
    store = getattr(writer, "store", None)
    idx: SourceFetchIndex | None = writer.source_fetch_idx
    after = int(writer.source_fetch_seq or 0)
    if store is None or not hasattr(store, "read_all"):
        return idx or SourceFetchIndex()
    if idx is None:
        idx = SourceFetchIndex()
        events = await store.read_all()
        after = apply_source_fetch_events(idx, events)
    else:
        if hasattr(store, "read_after"):
            events = await store.read_after(after)
        else:
            events = [
                e
                for e in await store.read_all()
                if int(getattr(e, "seq", 0) or 0) > after
            ]
        n = apply_source_fetch_events(idx, events)
        if n > after:
            after = n
    writer.source_fetch_idx = idx
    writer.source_fetch_seq = after
    return idx


def _cache_note_fetch(writer: StudyWriter, ev: EventEnvelope) -> None:
    idx = writer.source_fetch_idx
    if idx is None:
        return
    apply_source_fetch_events(idx, [ev])
    seq = int(getattr(ev, "seq", 0) or 0)
    if seq > int(writer.source_fetch_seq or 0):
        writer.source_fetch_seq = seq


async def append_source_fetched(
    writer: StudyWriter,
    *,
    study_id: str,
    call_id: str,
    tool: str,
    digest: str,
    url: str,
    capability: FetchCapability,
    on_event: Any | None = None,
) -> EventEnvelope:
    """Append ``SourceFetched`` v1. Requires bound (unspent) capability."""
    verify_fetch_capability(
        capability,
        tool=tool,
        call_id=call_id,
        study_id=study_id,
        attempt_id=_attempt_id_from_call_id(call_id),
        consume=False,
    )
    entity = f"{call_id}:{digest}"
    ev = EventEnvelope(
        study_id=study_id,
        type="SourceFetched",
        entity_key=entity,
        idem_key=make_idem("SourceFetched", entity),
        payload={
            "call_id": call_id,
            "tool": tool,
            "digest": digest,
            "url": url,
        },
        ts=datetime.now(UTC),
        schema_version=1,
    )
    stored = await writer.append(ev, _fetch_capability=capability)
    _cache_note_fetch(writer, stored)
    if on_event is not None:
        on_event(stored)
    return stored


async def store_fetched_source(
    writer: StudyWriter,
    artifacts: Any,
    content: bytes,
    *,
    study_id: str,
    call_id: str,
    tool: str,
    url: str,
    capability: FetchCapability,
    name: str = "source.txt",
    media_type: str = "text/plain",
    role: str = "paper",
    on_event: Any | None = None,
) -> Any:
    """``put_fetched`` + ``SourceFetched`` — executor-path only (capability required)."""
    from coscientist.core.artifacts import make_tool_ref

    verify_fetch_capability(
        capability,
        tool=tool,
        call_id=call_id,
        study_id=study_id,
        attempt_id=_attempt_id_from_call_id(call_id),
        consume=False,
    )
    art = artifacts.put_fetched(
        content,
        make_tool_ref(call_id, tool=tool, args=url),
        name=name,
        media_type=media_type,
        role=role,
        capability=capability,
        expected_tool=tool,
        expected_call_id=call_id,
        expected_study_id=study_id,
        expected_attempt_id=_attempt_id_from_call_id(call_id),
    )
    await append_source_fetched(
        writer,
        study_id=study_id,
        call_id=call_id,
        tool=tool,
        digest=art.sha256,
        url=url,
        capability=capability,
        on_event=on_event,
    )
    return art
