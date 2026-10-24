"""Journaled fetch provenance for fetch-tool outputs (M1 PR14 / K9).

``SourceFetched`` records that a CAS digest was produced by a registered
**fetch** tool (``RegisteredToolSpec.is_fetch``). It is **not** part of the
EffectIntended/EffectDone index. ``quote_in_source`` accepts a source only when
its digest appears here **for the evaluating attempt**.

Minting is **not** in this module — only ``coscientist.tools.effects`` holds
the issued-id registry. ``FetchCapability`` is an opaque handle (id only);
verify looks up the registry entry (tool/call/study/attempt) and consumes it.

Network content stays ``tainted=True``; journal match ≠ content trusted.
``countable=True`` with ``trusted=False`` is intentional (DESIGN_M1 §6.3 /
PR 8b). Retries and downstream tasks must re-fetch (attempt-scoped digests).

Payload v1: ``{call_id, tool, digest, url}``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from coscientist.core.events.errors import (
    ArtifactRejected,
    FetchToolReclassified,
    JournalFormatError,
)
from coscientist.core.events.events import (
    EventEnvelope,
    StoredEvent,
    UnstampedEvent,
    make_idem,
)
from coscientist.core.events.schema_versions import (
    ATTEMPT_TAINTED_SCHEMA_VERSION,
    FETCH_INTEGRITY_RAISED_SCHEMA_VERSION,
    SOURCE_FETCHED_SCHEMA_VERSION,
)
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.domain import Catalog, CodeCheck, RequirementRef, TaskSpec

VerifyFetchCap = Callable[..., "FetchCapability"]
AssertFetchCap = Callable[..., "FetchCapability"]
TakeWritePermit = Callable[..., None]
ReleaseWritePermit = Callable[[str], None]

# Installed by coscientist.tools.effects at import (registry never lives here).
_verify_impl: VerifyFetchCap | None = None
_assert_impl: AssertFetchCap | None = None
_take_write_permit_impl: TakeWritePermit | None = None
_release_write_permit_impl: ReleaseWritePermit | None = None


def register_fetch_capability_verifier(
    verify_fn: VerifyFetchCap,
    *,
    assert_fn: AssertFetchCap | None = None,
    take_write_permit_fn: TakeWritePermit | None = None,
    release_write_permit_fn: ReleaseWritePermit | None = None,
) -> None:
    """Called once from ``tools.effects`` — core never mints capabilities."""
    global _verify_impl, _assert_impl, _take_write_permit_impl, _release_write_permit_impl
    if _verify_impl is not None and _verify_impl is not verify_fn:
        raise RuntimeError("fetch capability verifier already registered")
    _verify_impl = verify_fn
    if assert_fn is not None:
        _assert_impl = assert_fn
    if take_write_permit_fn is not None:
        _take_write_permit_impl = take_write_permit_fn
    if release_write_permit_fn is not None:
        _release_write_permit_impl = release_write_permit_fn


def take_source_fetched_write_permit(
    cap_id: str,
    *,
    tool: str,
    call_id: str,
    study_id: str,
) -> None:
    """Consume the one-shot write permit for this capability id (effects registry)."""
    if _take_write_permit_impl is None:
        raise ArtifactRejected("fetch_capability_required")
    _take_write_permit_impl(
        cap_id, tool=tool, call_id=call_id, study_id=study_id
    )


def release_source_fetched_write_permit(cap_id: str) -> None:
    """Drop a write permit without journaling (append failed after verify)."""
    if _release_write_permit_impl is None:
        raise ArtifactRejected("fetch_capability_required")
    _release_write_permit_impl(cap_id)


@dataclass(frozen=True, slots=True)
class FetchCapability:
    """Opaque capability handle — only an id; binding lives in effects registry."""

    id: str


def verify_fetch_capability(
    capability: Any,
    *,
    tool: str | None = None,
    call_id: str | None = None,
    study_id: str | None = None,
    attempt_id: str | None = None,
) -> FetchCapability:
    """Lookup registry entry, check binds, **consume** (single-use)."""
    if _verify_impl is None:
        raise ArtifactRejected("fetch_capability_required")
    return _verify_impl(
        capability,
        tool=tool,
        call_id=call_id,
        study_id=study_id,
        attempt_id=attempt_id,
    )


def assert_fetch_capability_bound(
    capability: Any,
    *,
    tool: str,
    call_id: str,
    study_id: str,
    attempt_id: str,
) -> FetchCapability:
    """Lookup + bind check without consuming (put_fetched before SourceFetched)."""
    if _assert_impl is None:
        raise ArtifactRejected("fetch_capability_required")
    return _assert_impl(
        capability,
        tool=tool,
        call_id=call_id,
        study_id=study_id,
        attempt_id=attempt_id,
    )


def _optional_payload_str(payload: dict[str, Any], key: str) -> str:
    """Return payload[key] when a non-empty str; else empty (shape check fail)."""
    raw = payload.get(key)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        return ""
    return raw


def _event_seq(ev: Any) -> int:
    seq = ev.seq
    if seq is None:
        return 0
    if type(seq) is bool or not isinstance(seq, int):
        raise JournalFormatError(f"event seq must be int|None, got {type(seq).__name__}")
    return seq


def _writer_fetch_seq(writer: StudyWriter) -> int:
    raw = writer.source_fetch_seq
    if raw is None:
        return 0
    if type(raw) is bool or not isinstance(raw, int):
        raise JournalFormatError(
            f"source_fetch_seq must be int|None, got {type(raw).__name__}"
        )
    return raw


def task_needs_source_fetch(catalog: Catalog, spec: TaskSpec) -> bool:
    """True when evaluating this task may run ``quote_in_source`` (avoid journal scan)."""
    by_id = {t.id: t for t in catalog.templates}
    refs = list(spec.criteria)
    if not refs:
        if "experiment" not in catalog.defaults:
            raise JournalFormatError(
                "catalog.defaults missing required key 'experiment'"
            )
        for tid in catalog.defaults["experiment"]:
            refs.append(RequirementRef(template_id=tid, required=True))
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
    """True when SourceFetched payload has the fetch ToolCall shape fields."""
    call_id = _optional_payload_str(payload, "call_id")
    tool = _optional_payload_str(payload, "tool")
    digest = _optional_payload_str(payload, "digest")
    url = payload.get("url")
    if not call_id or ":" not in call_id:
        return False
    if not tool or not digest:
        return False
    if not isinstance(url, str):
        return False
    return True


def source_fetched_paired_with_fetch_call(
    payload: dict[str, Any],
    fetch_calls: dict[str, str],
) -> bool:
    """True when payload shape is ok and ``call_id`` maps to the same tool."""
    if not source_fetched_payload_ok(payload):
        return False
    call_id = _optional_payload_str(payload, "call_id")
    tool = _optional_payload_str(payload, "tool")
    return fetch_calls.get(call_id) == tool


@dataclass
class SourceFetchIndex:
    """Attempt-scoped digest sets (plus study-wide view for diagnostics)."""

    by_digest: dict[str, dict[str, Any]]
    by_attempt: dict[str, set[str]]
    fetch_calls: dict[str, str]  # call_id → tool (from AttemptTainted)
    # Reclassified same-name tools: do not count; journal FetchIntegrityRaised.
    reclassified: list[dict[str, str]]

    def __init__(self) -> None:
        self.by_digest = {}
        self.by_attempt = {}
        self.fetch_calls = {}
        self.reclassified = []

    def note_fetch_call(self, *, call_id: str, tool: str) -> None:
        if call_id and tool:
            self.fetch_calls[call_id] = tool

    def note_reclassified(self, *, call_id: str, tool: str) -> None:
        """Record integrity conflict; paired SourceFetched must not count."""
        if not call_id and not tool:
            return
        for row in self.reclassified:
            if row.get("call_id") == call_id and row.get("tool") == tool:
                return
        self.reclassified.append({"call_id": call_id, "tool": tool})

    def note(self, payload: dict[str, Any]) -> None:
        if not source_fetched_paired_with_fetch_call(payload, self.fetch_calls):
            return
        digest = _optional_payload_str(payload, "digest")
        call_id = _optional_payload_str(payload, "call_id")
        attempt_id = _attempt_id_from_call_id(call_id)
        self.by_digest[digest] = dict(payload)
        if attempt_id not in self.by_attempt:
            self.by_attempt[attempt_id] = set()
        self.by_attempt[attempt_id].add(digest)

    def has(self, digest: str) -> bool:
        return digest in self.by_digest

    def digests(self) -> frozenset[str]:
        """Study-wide digests (diagnostics only — checks use attempt scope)."""
        return frozenset(self.by_digest)

    def digests_for_attempt(self, attempt_id: str) -> frozenset[str]:
        if not attempt_id:
            return frozenset()
        bag = self.by_attempt.get(attempt_id)
        if bag is None:
            return frozenset()
        return frozenset(bag)


def note_fetch_call_from_attempt_tainted(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Resolve a fetch-call marker from ``AttemptTainted`` payload.

    Pairing trusts the **journaled** ``tool`` + ``is_fetch`` identity so renamed
    / removed tools do not silently drop old ``SourceFetched`` on replay.
    If the live registry still has the same name with ``is_fetch=False``, raise
    ``FetchToolReclassified`` (domain) — callers must catch, skip counting, and
    journal ``FetchIntegrityRaised`` so open/finalize stay possible.
    """
    if _optional_payload_str(payload, "reason") != "source_fetch":
        return None
    is_fetch = payload.get("is_fetch")
    if is_fetch is not True:
        return None
    tool = _optional_payload_str(payload, "tool")
    call_id = _optional_payload_str(payload, "call_id")
    if not tool or not call_id:
        return None
    from coscientist.core.execution.tool_registry import get_registered

    live = get_registered(tool)
    if live is not None and not live.is_fetch:
        raise FetchToolReclassified(tool, call_id)
    # Unregistered (renamed away) or still is_fetch — trust journaled identity.
    return call_id, tool


def apply_source_fetch_events(
    idx: SourceFetchIndex, events: list[Any]
) -> int:
    """Fold AttemptTainted fetch-calls then paired SourceFetched; return max seq.

    Reclassified tools are recorded on ``idx.reclassified`` and do **not** create
    fetch_calls, so subsequent ``SourceFetched`` for that call is not counted.
    """
    max_seq = 0
    for ev in events:
        seq = _event_seq(ev)
        if seq > max_seq:
            max_seq = seq
        t = ev.type
        p = ev.payload
        if p is None:
            continue
        if not isinstance(p, dict):
            continue
        if t == "AttemptTainted":
            try:
                marked = note_fetch_call_from_attempt_tainted(p)
            except FetchToolReclassified as exc:
                idx.note_reclassified(call_id=exc.call_id, tool=exc.tool)
                continue
            if marked is not None:
                call_id, tool = marked
                idx.note_fetch_call(call_id=call_id, tool=tool)
        elif t == "SourceFetched":
            idx.note(p)
    return max_seq


def load_source_fetch_index(events: list[Any]) -> SourceFetchIndex:
    idx = SourceFetchIndex()
    apply_source_fetch_events(idx, events)
    return idx


async def journal_fetch_integrity_raised(
    writer: StudyWriter,
    issues: list[dict[str, str]],
    *,
    study_id: str,
    on_event: Any | None = None,
) -> list[StoredEvent]:
    """Append idempotent ``FetchIntegrityRaised`` for each reclassified call."""
    from coscientist.core.events.errors import Idempotent

    out = []
    for issue in issues:
        tool = _optional_payload_str(issue, "tool")
        call_id = _optional_payload_str(issue, "call_id")
        if not tool and not call_id:
            continue
        entity = call_id if call_id else tool
        ev = EventEnvelope(
            study_id=study_id,
            type="FetchIntegrityRaised",
            entity_key=entity,
            idem_key=make_idem("FetchIntegrityRaised", entity, tool),
            payload={
                "kind": "fetch_tool_reclassified",
                "tool": tool,
                "call_id": call_id,
                "internal_error": True,
            },
            ts=datetime.now(UTC),
            schema_version=FETCH_INTEGRITY_RAISED_SCHEMA_VERSION,
        )
        try:
            stored = await writer.append(ev)
        except Idempotent:
            continue
        out.append(stored)
        seq = _event_seq(stored)
        if seq > _writer_fetch_seq(writer):
            writer.source_fetch_seq = seq
        if on_event is not None:
            on_event(stored)
    return out


async def reconcile_fetch_integrity(writer: StudyWriter) -> SourceFetchIndex:
    """Fold journal, journal any reclassified markers, return index (uncounted).

    Does **not** append when the study is already finalized — ``FetchIntegrityRaised``
    must not land after ``StudyFinalized``.
    """
    store = writer.store
    if store is None:
        return SourceFetchIndex()
    events = await store.read_all()
    idx = SourceFetchIndex()
    apply_source_fetch_events(idx, events)
    study_id = ""
    if events:
        study_id = events[0].study_id
        if not isinstance(study_id, str):
            raise JournalFormatError(
                f"event study_id must be str, got {type(study_id).__name__}"
            )
    if not study_id:
        study_id = store.study_id
        if not isinstance(study_id, str):
            raise JournalFormatError(
                f"store.study_id must be str, got {type(study_id).__name__}"
            )
    finalized = any(e.type == "StudyFinalized" for e in events)
    if finalized:
        # Fold only — never journal past StudyFinalized.
        writer.source_fetch_idx = idx
        if events:
            writer.source_fetch_seq = max(_event_seq(e) for e in events)
        return idx
    if idx.reclassified and study_id:
        await journal_fetch_integrity_raised(
            writer, idx.reclassified, study_id=study_id
        )
        # Refresh cache after diagnostic appends.
        writer.source_fetch_idx = None
        writer.source_fetch_seq = 0
        return await load_source_fetch_index_from_writer(writer)
    writer.source_fetch_idx = idx
    if events:
        writer.source_fetch_seq = max(_event_seq(e) for e in events)
    return idx


async def load_source_fetch_index_from_writer(writer: StudyWriter) -> SourceFetchIndex:
    """Incremental fold cached on the writer (O(Δ) after first load)."""
    store = writer.store
    idx: SourceFetchIndex | None = writer.source_fetch_idx
    after = _writer_fetch_seq(writer)
    if store is None:
        if idx is None:
            return SourceFetchIndex()
        return idx
    if idx is None:
        idx = SourceFetchIndex()
        events = await store.read_all()
        after = apply_source_fetch_events(idx, events)
    else:
        try:
            events = await store.read_after(after)
        except AttributeError:
            events = [e for e in await store.read_all() if _event_seq(e) > after]
        n = apply_source_fetch_events(idx, events)
        if n > after:
            after = n
    writer.source_fetch_idx = idx
    writer.source_fetch_seq = after
    # Read path only: never journal here (executor evaluate). Writes belong to
    # ``reconcile_fetch_integrity`` on open/finalize.
    return idx


def _cache_note_fetch(writer: StudyWriter, ev: StoredEvent | UnstampedEvent) -> None:
    idx = writer.source_fetch_idx
    if idx is None:
        return
    apply_source_fetch_events(idx, [ev])
    seq = _event_seq(ev)
    if seq > _writer_fetch_seq(writer):
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
) -> StoredEvent:
    """Append ``SourceFetched`` v1. Consumes the opaque capability (single-use)."""
    verify_fetch_capability(
        capability,
        tool=tool,
        call_id=call_id,
        study_id=study_id,
        attempt_id=_attempt_id_from_call_id(call_id),
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
        schema_version=SOURCE_FETCHED_SCHEMA_VERSION,
    )
    # verify granted a write permit — release it if append fails so it never leaks.
    try:
        stored = await writer.append(ev, _source_fetched_cap_id=capability.id)
    except BaseException:
        release_source_fetched_write_permit(capability.id)
        raise
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
    name: str,
    media_type: str,
    role: str,
    on_event: Any | None = None,
) -> Any:
    """``put_fetched`` + ``SourceFetched`` — executor-path only (capability required).

    Journals ``AttemptTainted`` first so SourceFetched pairs with the fetch call
    (same marker ``invoke_tool`` writes before an ``is_fetch`` call).
    """
    from coscientist.core.execution.artifacts import make_tool_ref

    attempt_id = _attempt_id_from_call_id(call_id)
    # Fetch-call marker for projection / index pairing (idempotent per call_id).
    tainted = EventEnvelope(
        study_id=study_id,
        type="AttemptTainted",
        entity_key=attempt_id,
        idem_key=make_idem("AttemptTainted", attempt_id, call_id),
        payload={
            "attempt_id": attempt_id,
            "reason": "source_fetch",
            "call_id": call_id,
            "tool": tool,
            "is_fetch": True,
        },
        ts=datetime.now(UTC),
        schema_version=ATTEMPT_TAINTED_SCHEMA_VERSION,
    )
    stored_taint = await writer.append(tainted)
    _cache_note_fetch(writer, stored_taint)
    if on_event is not None:
        on_event(stored_taint)

    # put asserts bound (no consume); append verifies+consumes.
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
        expected_attempt_id=attempt_id,
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
