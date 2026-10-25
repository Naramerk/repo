"""effect_idem_key + EffectIntended/EffectDone invoke path (DESIGN_M1 §8).

``invoke_tool`` is the **only** allowed path for tool calls that may have
side effects. Effect class comes from the closed core registry
(``resolve_effect``), never from a caller-supplied ``ToolSpec.effect``.
Unregistered tools are treated as ``external_effect`` (fail-closed).

Read tools skip EffectIntended/Done (TG-Call still journals ``ToolCalled``).
Write / external_effect keys include ``attempt_id`` for completed effects,
but the in-doubt check (Intended-without-Done) spans **all** attempts for
the same (study, task_key, tool, args_digest) fingerprint.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import ArtifactRejected
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.payload_schemas import ToolCalledPayload
from coscientist.core.events.schema_versions import TOOL_CALLED_SCHEMA_VERSION
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import ToolCallRef
from coscientist.core.execution.source_fetch import (
    FetchCapability,
    register_fetch_capability_verifier,
)
from coscientist.core.execution.taint import mark_attempt_tainted
from coscientist.core.execution.tool_registry import (
    resolve_effect,
    resolve_is_fetch,
    resolve_spec,
)
from coscientist.tools.contract import CallCtx, Tool, ToolError, ToolOutput
from coscientist.tools.gate import side_effect_allowed

# Issued capability registry — private to this module (AST + import-linter).
# FetchCapability is an opaque id; binding fields live only here.


@dataclass(slots=True)
class _IssuedFetchCap:
    tool: str
    call_id: str
    study_id: str
    attempt_id: str


_ISSUED_FETCH_CAPS: dict[str, _IssuedFetchCap] = {}
# After verify consumes an issued id, a one-shot write permit for SourceFetched
# remains until store/writer takes it (bound to the same capability id).
_SOURCE_FETCHED_WRITE_PERMITS: dict[str, _IssuedFetchCap] = {}


def _mint_fetch_capability(
    *,
    tool: str,
    call_id: str,
    study_id: str,
    attempt_id: str,
) -> FetchCapability:
    """Only ``invoke_tool`` (this module) may mint fetch capabilities."""
    cap_id = secrets.token_urlsafe(32)
    _ISSUED_FETCH_CAPS[cap_id] = _IssuedFetchCap(
        tool=tool,
        call_id=call_id,
        study_id=study_id,
        attempt_id=attempt_id,
    )
    return FetchCapability(id=cap_id)


def _mint_fetch_capability_for_tests(
    *,
    tool: str,
    call_id: str,
    study_id: str = "st_test",
    attempt_id: str | None = None,
) -> FetchCapability:
    """Test-only mint (AST-banned outside ``tests/``)."""
    aid = attempt_id if attempt_id is not None else (
        call_id.rsplit(":", 1)[0] if ":" in call_id else call_id
    )
    return _mint_fetch_capability(
        tool=tool, call_id=call_id, study_id=study_id, attempt_id=aid
    )


def _invalidate_fetch_capability(capability: FetchCapability | None) -> None:
    if capability is None:
        return
    cap_id = getattr(capability, "id", None) or ""
    _ISSUED_FETCH_CAPS.pop(cap_id, None)
    _SOURCE_FETCHED_WRITE_PERMITS.pop(cap_id, None)


def _lookup_issued(
    capability: Any,
    *,
    tool: str | None,
    call_id: str | None,
    study_id: str | None,
    attempt_id: str | None,
) -> tuple[FetchCapability, _IssuedFetchCap]:
    if not isinstance(capability, FetchCapability):
        raise ArtifactRejected("fetch_capability_required")
    entry = _ISSUED_FETCH_CAPS.get(capability.id)
    if entry is None:
        raise ArtifactRejected("fetch_capability_required")
    if tool is not None and entry.tool != tool:
        raise ArtifactRejected("fetch_capability_mismatch")
    if call_id is not None and entry.call_id != call_id:
        raise ArtifactRejected("fetch_capability_mismatch")
    if study_id is not None and entry.study_id != study_id:
        raise ArtifactRejected("fetch_capability_mismatch")
    if attempt_id is not None and entry.attempt_id != attempt_id:
        raise ArtifactRejected("fetch_capability_mismatch")
    return capability, entry


def _assert_fetch_capability(
    capability: Any,
    *,
    tool: str | None = None,
    call_id: str | None = None,
    study_id: str | None = None,
    attempt_id: str | None = None,
) -> FetchCapability:
    """Bind check without consuming (put_fetched before SourceFetched)."""
    cap, _entry = _lookup_issued(
        capability,
        tool=tool,
        call_id=call_id,
        study_id=study_id,
        attempt_id=attempt_id,
    )
    return cap


def _verify_fetch_capability(
    capability: Any,
    *,
    tool: str | None = None,
    call_id: str | None = None,
    study_id: str | None = None,
    attempt_id: str | None = None,
) -> FetchCapability:
    """Bind check, consume issued id, grant one-shot SourceFetched write permit."""
    cap, entry = _lookup_issued(
        capability,
        tool=tool,
        call_id=call_id,
        study_id=study_id,
        attempt_id=attempt_id,
    )
    _ISSUED_FETCH_CAPS.pop(cap.id, None)
    _SOURCE_FETCHED_WRITE_PERMITS[cap.id] = entry
    return cap


def _take_source_fetched_write_permit(
    cap_id: str,
    *,
    tool: str,
    call_id: str,
    study_id: str,
) -> None:
    """Consume the one-shot write permit granted by verify (effects-private).

    Bind mismatches raise without consuming so a correct retry can still take.
    """
    entry = _SOURCE_FETCHED_WRITE_PERMITS.get(cap_id)
    if entry is None:
        raise ArtifactRejected("fetch_capability_required")
    if entry.tool != tool or entry.call_id != call_id or entry.study_id != study_id:
        raise ArtifactRejected("fetch_capability_mismatch")
    _SOURCE_FETCHED_WRITE_PERMITS.pop(cap_id, None)


def _release_source_fetched_write_permit(cap_id: str) -> None:
    """Drop write permit without take (append failed after verify)."""
    _SOURCE_FETCHED_WRITE_PERMITS.pop(cap_id, None)


register_fetch_capability_verifier(
    _verify_fetch_capability,
    assert_fn=_assert_fetch_capability,
    take_write_permit_fn=_take_source_fetched_write_permit,
    release_write_permit_fn=_release_source_fetched_write_permit,
)


def effect_fingerprint(
    *,
    study_id: str,
    task_key: str,
    tool: str,
    args_digest: str,
) -> str:
    """Cross-attempt identity for in-doubt / exactly-once checks."""
    return sha256_hex(
        canonical_json(
            {
                "study_id": study_id,
                "task_key": task_key,
                "tool": tool,
                "args_digest": args_digest,
            }
        )
    )


def effect_idem_key(
    *,
    study_id: str,
    task_key: str,
    tool: str,
    args_digest: str,
    attempt_id: str,
) -> str:
    """sha256(JCS({study_id, task_key, tool, args_digest, attempt_id})).

    Completed effects are keyed per attempt. In-doubt detection uses
    ``effect_fingerprint`` (no attempt) across the whole journal.
    """
    return sha256_hex(
        canonical_json(
            {
                "study_id": study_id,
                "task_key": task_key,
                "tool": tool,
                "args_digest": args_digest,
                "attempt_id": attempt_id,
            }
        )
    )


def args_digest(args: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(args))


@dataclass
class EffectIndex:
    """Folded EffectIntended / EffectDone rows keyed by effect_idem_key."""

    intended: dict[str, dict[str, Any]] = field(default_factory=dict)
    done: dict[str, dict[str, Any]] = field(default_factory=dict)

    def note_intended(self, key: str, payload: dict[str, Any]) -> None:
        self.intended[key] = dict(payload)

    def note_done(self, key: str, payload: dict[str, Any]) -> None:
        self.intended.setdefault(key, {})
        self.done[key] = dict(payload)

    def unknown_keys(self) -> list[str]:
        return sorted(set(self.intended) - set(self.done))

    def in_doubt_for_fingerprint(
        self,
        *,
        study_id: str,
        task_key: str,
        tool: str,
        args_digest: str,
    ) -> dict[str, Any] | None:
        """Any Intended-without-Done matching the fingerprint (any attempt)."""
        from coscientist.core.events.attempt_fields import require_attempt_str

        for key, payload in self.intended.items():
            if key in self.done:
                continue
            # study_id may carry AttemptUnrecorded after legacy upcast — never
            # coerce the marker to a string for fingerprint match.
            raw_sid = payload.get("study_id")
            if raw_sid is None:
                continue
            got_study = require_attempt_str(raw_sid, field="study_id")
            if (
                got_study == study_id
                and str(payload.get("task_key") or "") == task_key
                and str(payload.get("tool") or "") == tool
                and str(payload.get("args_digest") or "") == args_digest
            ):
                return dict(payload)
        return None


def load_effect_index(events: list[EventEnvelope] | list[Any]) -> EffectIndex:
    idx = EffectIndex()
    for ev in events:
        t = getattr(ev, "type", None) or (ev.get("type") if isinstance(ev, dict) else None)
        p = getattr(ev, "payload", None) or (ev.get("payload") if isinstance(ev, dict) else {})
        if not isinstance(p, dict):
            continue
        key = str(p.get("key") or getattr(ev, "entity_key", "") or "")
        if not key:
            continue
        if t == "EffectIntended":
            idx.note_intended(key, p)
        elif t == "EffectDone":
            idx.note_done(key, p)
    return idx


def _apply_events(idx: EffectIndex, events: list[Any]) -> int:
    """Fold events into idx; return max seq seen (0 if none)."""
    max_seq = 0
    for ev in events:
        seq = int(getattr(ev, "seq", None) or 0)
        if seq > max_seq:
            max_seq = seq
        t = getattr(ev, "type", None)
        p = getattr(ev, "payload", None) or {}
        if not isinstance(p, dict):
            continue
        key = str(p.get("key") or getattr(ev, "entity_key", "") or "")
        if not key:
            continue
        if t == "EffectIntended":
            idx.note_intended(key, p)
        elif t == "EffectDone":
            idx.note_done(key, p)
    return max_seq


async def _load_index_from_writer(writer: StudyWriter) -> EffectIndex:
    """Incremental journal fold cached on the writer (avoids O(N) full re-read)."""
    store = getattr(writer, "store", None)
    if store is None or not hasattr(store, "read_all"):
        return EffectIndex()
    idx: EffectIndex | None = getattr(writer, "_pr18_effect_idx", None)
    after = int(getattr(writer, "_pr18_effect_seq", 0) or 0)
    if idx is None:
        idx = EffectIndex()
        events = await store.read_all()
        after = _apply_events(idx, events)
    else:
        if hasattr(store, "read_after"):
            events = await store.read_after(after)
        else:
            events = [e for e in await store.read_all() if int(getattr(e, "seq", 0) or 0) > after]
        n = _apply_events(idx, events)
        if n > after:
            after = n
    writer._pr18_effect_idx = idx  # type: ignore[attr-defined]
    writer._pr18_effect_seq = after  # type: ignore[attr-defined]
    return idx


def _cache_note(writer: StudyWriter, ev: EventEnvelope) -> None:
    idx: EffectIndex | None = getattr(writer, "_pr18_effect_idx", None)
    if idx is None:
        return
    _apply_events(idx, [ev])
    seq = int(getattr(ev, "seq", 0) or 0)
    cur = int(getattr(writer, "_pr18_effect_seq", 0) or 0)
    if seq > cur:
        writer._pr18_effect_seq = seq  # type: ignore[attr-defined]


def _note_attempt_taint(cache: dict[str, bool], ev: Any) -> None:
    t = getattr(ev, "type", None)
    p = getattr(ev, "payload", None) or {}
    if not isinstance(p, dict):
        return
    aid = str(p.get("attempt_id") or getattr(ev, "entity_key", "") or "")
    if not aid:
        return
    if t == "AttemptStarted":
        # Sticky OR: later AttemptTainted must not be cleared by replay order.
        cache[aid] = bool(cache.get(aid)) or bool(p.get("tainted_context"))
    elif t == "AttemptTainted":
        cache[aid] = True


async def _tainted_from_journal(writer: StudyWriter, attempt_id: str) -> bool:
    """Read attempt taint from journal (AttemptStarted + AttemptTainted).

    Incremental fold cached on the writer (same pattern as the effect index) so
    each ``invoke_tool`` is O(Δ) rather than a full ``read_all``.

    Fail-closed: missing store or unknown ``attempt_id`` → tainted (deny writes).
    """
    store = getattr(writer, "store", None)
    if store is None or not hasattr(store, "read_all"):
        return True
    if not attempt_id:
        return True
    cache: dict[str, bool] | None = getattr(writer, "_pr18_taint_by_attempt", None)
    after = int(getattr(writer, "_pr18_taint_seq", 0) or 0)
    if cache is None:
        cache = {}
        events = await store.read_all()
        max_seq = 0
        for ev in events:
            seq = int(getattr(ev, "seq", 0) or 0)
            if seq > max_seq:
                max_seq = seq
            _note_attempt_taint(cache, ev)
        after = max_seq
    else:
        # Always fold Δ — AttemptTainted may arrive after AttemptStarted cached.
        if hasattr(store, "read_after"):
            events = await store.read_after(after)
        else:
            events = [
                e
                for e in await store.read_all()
                if int(getattr(e, "seq", 0) or 0) > after
            ]
        for ev in events:
            seq = int(getattr(ev, "seq", 0) or 0)
            if seq > after:
                after = seq
            _note_attempt_taint(cache, ev)
    writer._pr18_taint_by_attempt = cache  # type: ignore[attr-defined]
    writer._pr18_taint_seq = after  # type: ignore[attr-defined]
    if attempt_id not in cache:
        return True  # unknown attempt — fail closed
    return bool(cache[attempt_id])


async def _journal_attempt_tainted(
    writer: StudyWriter,
    *,
    study_id: str,
    attempt_id: str,
    call_id: str,
    tool: str,
    on_event: Any | None = None,
) -> EventEnvelope:
    """Journal sticky attempt taint after a successful source fetch."""
    ev = EventEnvelope(
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
        schema_version=1,
    )
    stored = await writer.append(ev)
    cache: dict[str, bool] | None = getattr(writer, "_pr18_taint_by_attempt", None)
    if cache is not None:
        cache[attempt_id] = True
    # Keep SourceFetchIndex cache in sync when open/finalize warmed it — otherwise
    # a later SourceFetched Δ fold can advance seq past this AttemptTainted and
    # leave fetch_calls empty (quote_in_source would miss the digest).
    from coscientist.core.execution.source_fetch import _cache_note_fetch

    _cache_note_fetch(writer, stored)
    if on_event is not None:
        on_event(stored)
    return stored


def _restore_output(
    arts: LocalArtifactStore | None,
    done: dict[str, Any],
) -> ToolOutput:
    """Return the stored result or raise — never a stub ``{restored: True}``."""
    status = str(done.get("status") or "ok")
    if status == "failed":
        kind = str(done.get("error_kind") or "opaque")
        detail = str(done.get("detail") or "EffectDone(failed)")
        raise ToolError(kind, detail)  # type: ignore[arg-type]
    result_ref = done.get("result_ref")
    if not result_ref:
        raise ToolError("opaque", "EffectDone missing result_ref")
    if arts is None:
        raise ToolError(
            "opaque",
            "EffectDone has result_ref but artifact store is not attached",
        )
    raw = arts.read(str(result_ref))
    payload = json.loads(raw.decode("utf-8"))
    return ToolOutput.model_validate(payload.get("output") or payload)


async def _append(
    writer: StudyWriter,
    ev: EventEnvelope,
    on_event: Any | None,
) -> EventEnvelope:
    stored = await writer.append(ev)
    _cache_note(writer, stored)
    if on_event is not None:
        on_event(stored)
    return stored


async def _journal_tool_called(
    writer: StudyWriter,
    *,
    study_id: str,
    call_id: str,
    tool: str,
    args_digest: str,
    started: datetime,
    ended: datetime,
    outcome: Literal["ok", "error"],
    error_kind: str | None,
    on_event: Any | None,
) -> None:
    """DESIGN §10 ToolCalled — fail-closed WireModel before append."""
    ms = int((ended - started).total_seconds() * 1000)
    if ms < 0:
        raise ToolError("local", f"ToolCalled duration_ms negative: {ms}")
    payload = ToolCalledPayload(
        ref=ToolCallRef(
            call_id=call_id,
            tool=tool,
            args_digest=args_digest,
            started_at=started,
            ended_at=ended,
        ),
        outcome=outcome,
        error_kind=error_kind,
        duration_ms=ms,
    )
    # call_id alone is not unique: read tools reuse call_seq across invokes.
    await _append(
        writer,
        EventEnvelope(
            study_id=study_id,
            type="ToolCalled",
            entity_key=call_id,
            idem_key=make_idem(
                "ToolCalled",
                call_id,
                started.isoformat(),
                outcome,
                error_kind or "-",
                str(ms),
            ),
            payload=payload.model_dump(mode="json"),
            ts=ended,
            schema_version=TOOL_CALLED_SCHEMA_VERSION,
        ),
        on_event,
    )


async def _call_and_journal(
    tool: Tool,
    args: dict[str, Any],
    ctx: CallCtx,
    *,
    writer: StudyWriter,
    name: str,
    digest: str,
    call_id: str,
    on_event: Any | None,
) -> ToolOutput:
    """Invoke ``tool.call`` and journal ToolCalled for ok and error outcomes."""
    started = datetime.now(UTC)
    outcome: Literal["ok", "error"] = "ok"
    error_kind: str | None = None
    output: ToolOutput | None = None
    pending: BaseException | None = None
    try:
        output = await tool.call(args, ctx)
    except ToolError as err:
        outcome, error_kind, pending = "error", err.kind, err
    except Exception as exc:  # noqa: BLE001 — surface as opaque tool failure
        outcome, error_kind = "error", "opaque"
        pending = ToolError("opaque", f"tool raised: {type(exc).__name__}: {exc}")
        pending.__cause__ = exc
    ended = datetime.now(UTC)
    await _journal_tool_called(
        writer,
        study_id=ctx.study_id,
        call_id=call_id,
        tool=name,
        args_digest=digest,
        started=started,
        ended=ended,
        outcome=outcome,
        error_kind=error_kind,
        on_event=on_event,
    )
    if pending is not None:
        raise pending
    assert output is not None
    return output


async def _deny(
    writer: StudyWriter,
    *,
    ctx: CallCtx,
    tool: str,
    effect: str,
    digest: str,
    reason: str,
    on_event: Any | None,
) -> None:
    deny_key = effect_idem_key(
        study_id=ctx.study_id,
        task_key=ctx.task_key,
        tool=tool,
        args_digest=digest,
        attempt_id=ctx.attempt_id,
    )
    await _append(
        writer,
        EventEnvelope(
            study_id=ctx.study_id,
            type="SideEffectDenied",
            entity_key=deny_key,
            idem_key=make_idem("SideEffectDenied", deny_key, reason),
            payload={
                "key": deny_key,
                "tool": tool,
                "task_key": ctx.task_key,
                "attempt_id": ctx.attempt_id,
                "effect": effect,
                "reason": reason,
            },
            ts=datetime.now(UTC),
            schema_version=1,
        ),
        on_event,
    )


async def invoke_tool(
    tool: Tool,
    args: dict[str, Any],
    ctx: CallCtx,
    *,
    writer: StudyWriter,
    artifacts: LocalArtifactStore | None = None,
    effect_index: EffectIndex | None = None,
    on_event: Any | None = None,
) -> ToolOutput:
    """Call a tool under the closed registry + side_effect gate + effect protocol.

    Effect is resolved from ``coscientist.core.execution.tool_registry`` by tool name.
    Unregistered names → ``external_effect``. Read calls skip the effect journal.
    Taint is read from ``AttemptStarted.tainted_context`` in the journal.
    """
    name = tool.spec.name
    reg = resolve_spec(name)
    effect = resolve_effect(name)  # authoritative; ignore tool.spec.effect
    digest = args_digest(args)

    # --- read: no EffectIntended/Done; SourceFetched only for is_fetch tools ---
    if effect == "read":
        # Authoritative registry flag — never ToolSpec / catalog / getattr default.
        is_fetch = resolve_is_fetch(name)
        call_id = f"{ctx.attempt_id}:{ctx.call_seq}"
        minted: list[FetchCapability] = []
        try:
            if is_fetch:
                # Taint BEFORE the call — any is_fetch attempt, success or fail.
                await _journal_attempt_tainted(
                    writer,
                    study_id=ctx.study_id,
                    attempt_id=ctx.attempt_id,
                    call_id=call_id,
                    tool=name,
                    on_event=on_event,
                )
                mark_attempt_tainted()
                cap = _mint_fetch_capability(
                    tool=name,
                    call_id=call_id,
                    study_id=ctx.study_id,
                    attempt_id=ctx.attempt_id,
                )
                minted.append(cap)
                ctx.fetch_capability = cap
            else:
                ctx.fetch_capability = None

            output = await _call_and_journal(
                tool,
                args,
                ctx,
                writer=writer,
                name=name,
                digest=digest,
                call_id=call_id,
                on_event=on_event,
            )

            # Provenance for quote_in_source: only explicit fetch tools, via
            # capability-gated put_fetched + SourceFetched (not any read tool).
            if (
                is_fetch
                and minted
                and artifacts is not None
                and output.artifacts
            ):
                from coscientist.core.execution.source_fetch import append_source_fetched

                # URL from normalized tool result — never raw agent args.
                url = ""
                if isinstance(output.data, dict):
                    url = str(
                        output.data.get("url") or output.data.get("uri") or ""
                    )
                # First digest consumes the capability; further digests need a
                # fresh mint (single-use verify). Track all mints for finally.
                first = True
                for art in output.artifacts:
                    if getattr(art, "producer", None) != "tool":
                        continue
                    art_digest = str(getattr(art, "sha256", "") or "")
                    if not art_digest:
                        continue
                    if first:
                        use_cap = minted[0]
                    else:
                        use_cap = _mint_fetch_capability(
                            tool=name,
                            call_id=call_id,
                            study_id=ctx.study_id,
                            attempt_id=ctx.attempt_id,
                        )
                        minted.append(use_cap)
                    await append_source_fetched(
                        writer,
                        study_id=ctx.study_id,
                        call_id=call_id,
                        tool=name,
                        digest=art_digest,
                        url=url,
                        capability=use_cap,
                        on_event=on_event,
                    )
                    first = False
            return output
        finally:
            # Drop every minted id (issued + leftover write permits) on any path.
            for c in minted:
                _invalidate_fetch_capability(c)
            ctx.fetch_capability = None

    # Write/external_effect require an artifact store up front (before the call).
    if artifacts is None:
        raise ToolError(
            "local",
            "artifact store required to record EffectDone for write/external_effect",
        )

    # Taint from journal AttemptStarted — never from caller ctx fields.
    tainted = await _tainted_from_journal(writer, ctx.attempt_id)
    ctx.tainted_context = tainted

    if tainted:
        await _deny(
            writer,
            ctx=ctx,
            tool=name,
            effect=effect,
            digest=digest,
            reason="taint_deny",
            on_event=on_event,
        )
        raise ToolError(
            "local",
            "tainted context: write/external_effect tools are not available",
        )
    if not side_effect_allowed(ctx.hitl_policy):
        await _deny(
            writer,
            ctx=ctx,
            tool=name,
            effect=effect,
            digest=digest,
            reason="side_effect_deny",
            on_event=on_event,
        )
        raise ToolError(
            "local",
            "side_effect gate denied (headless default deny)",
        )

    key = effect_idem_key(
        study_id=ctx.study_id,
        task_key=ctx.task_key,
        tool=name,
        args_digest=digest,
        attempt_id=ctx.attempt_id,
    )
    idx = await _load_index_from_writer(writer)
    if effect_index is not None:
        for k, v in effect_index.done.items():
            idx.done.setdefault(k, v)
        for k, v in effect_index.intended.items():
            idx.intended.setdefault(k, v)

    # Same-attempt Done → restore.
    done = idx.done.get(key)
    if done is not None:
        return _restore_output(artifacts, done)

    # Cross-attempt in-doubt: any Intended-without-Done with same fingerprint.
    # Never invoke again — status unknown (exactly-once). supports_idem_key only
    # allows re-call when the dangling Intended is for THIS attempt_id.
    doubt = idx.in_doubt_for_fingerprint(
        study_id=ctx.study_id,
        task_key=ctx.task_key,
        tool=name,
        args_digest=digest,
    )
    if doubt is not None:
        same_attempt = str(doubt.get("attempt_id") or "") == ctx.attempt_id
        if same_attempt and reg.supports_idem_key:
            pass  # re-call with server-side idem_key
        else:
            raise ToolError(
                "opaque",
                "EffectIntended without EffectDone; status unknown "
                f"(attempt={doubt.get('attempt_id')!r})",
            )

    now = datetime.now(UTC)
    await _append(
        writer,
        EventEnvelope(
            study_id=ctx.study_id,
            type="EffectIntended",
            entity_key=key,
            idem_key=make_idem("EffectIntended", key),
            payload={
                "key": key,
                "study_id": ctx.study_id,
                "task_key": ctx.task_key,
                "tool": name,
                "args_digest": digest,
                "effect": effect,
                "attempt_id": ctx.attempt_id,
                "fingerprint": effect_fingerprint(
                    study_id=ctx.study_id,
                    task_key=ctx.task_key,
                    tool=name,
                    args_digest=digest,
                ),
            },
            ts=now,
            schema_version=1,
        ),
        on_event,
    )
    idx.note_intended(
        key,
        {
            "key": key,
            "tool": name,
            "attempt_id": ctx.attempt_id,
            "study_id": ctx.study_id,
            "task_key": ctx.task_key,
            "args_digest": digest,
        },
    )

    if reg.supports_idem_key:
        ctx.extra["effect_idem_key"] = key

    call_id = f"{ctx.attempt_id}:{ctx.call_seq}"
    try:
        output = await _call_and_journal(
            tool,
            args,
            ctx,
            writer=writer,
            name=name,
            digest=digest,
            call_id=call_id,
            on_event=on_event,
        )
    except ToolError as err:
        if err.kind == "transport_unsent":
            await _append(
                writer,
                EventEnvelope(
                    study_id=ctx.study_id,
                    type="EffectDone",
                    entity_key=key,
                    idem_key=make_idem("EffectDone", key),
                    payload={
                        "key": key,
                        "result_ref": None,
                        "tool": name,
                        "attempt_id": ctx.attempt_id,
                        "status": "failed",
                        "error_kind": "transport_unsent",
                        "detail": err.detail,
                    },
                    ts=datetime.now(UTC),
                    schema_version=1,
                ),
                on_event,
            )
            idx.note_done(key, {"key": key, "status": "failed"})
        raise

    if output.artifacts:
        tainted_arts = [
            a if a.tainted else a.model_copy(update={"tainted": True})
            for a in output.artifacts
        ]
        output = output.model_copy(update={"artifacts": tainted_arts})

    blob = canonical_json({"output": output.model_dump(mode="json"), "key": key})
    ref = make_tool_ref(
        f"{ctx.attempt_id}:{ctx.call_seq}",
        tool=name,
        args=digest,
    )
    art = artifacts.put_tool(
        blob,
        ref,
        name=f"effect:{name}",
        media_type="application/json",
        context_tainted=tainted,
    )
    result_ref = art.artifact_id

    done_payload = {
        "key": key,
        "result_ref": result_ref,
        "tool": name,
        "attempt_id": ctx.attempt_id,
        "status": "ok",
    }
    await _append(
        writer,
        EventEnvelope(
            study_id=ctx.study_id,
            type="EffectDone",
            entity_key=key,
            idem_key=make_idem("EffectDone", key),
            payload=done_payload,
            ts=datetime.now(UTC),
            schema_version=1,
        ),
        on_event,
    )
    idx.note_done(key, done_payload)
    return output
