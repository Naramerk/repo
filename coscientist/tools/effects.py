"""effect_idem_key + EffectIntended/EffectDone invoke path (DESIGN_M1 §8).

``invoke_tool`` is the **only** allowed path for tool calls that may have
side effects. Effect class comes from the closed core registry
(``resolve_effect``), never from a caller-supplied ``ToolSpec.effect``.
Unregistered tools are treated as ``external_effect`` (fail-closed).

Read tools are not journaled. Write / external_effect keys include
``attempt_id`` for completed effects, but the in-doubt check
(Intended-without-Done) spans **all** attempts for the same
(study, task_key, tool, args_digest) fingerprint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.tool_registry import resolve_effect, resolve_spec
from coscientist.core.writer import StudyWriter
from coscientist.tools.contract import CallCtx, Tool, ToolError, ToolOutput
from coscientist.tools.gate import side_effect_allowed


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
        for key, payload in self.intended.items():
            if key in self.done:
                continue
            if (
                str(payload.get("study_id") or "") == study_id
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
    if getattr(ev, "type", None) != "AttemptStarted":
        return
    p = getattr(ev, "payload", None) or {}
    if not isinstance(p, dict):
        return
    aid = str(p.get("attempt_id") or getattr(ev, "entity_key", "") or "")
    if aid:
        cache[aid] = bool(p.get("tainted_context"))


async def _tainted_from_journal(writer: StudyWriter, attempt_id: str) -> bool:
    """Read ``AttemptStarted.tainted_context`` for this attempt from the journal.

    Incremental fold cached on the writer (same pattern as the effect index) so
    each ``invoke_tool`` is O(Δ) rather than a full ``read_all``.
    """
    store = getattr(writer, "store", None)
    if store is None or not hasattr(store, "read_all"):
        return False
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
    elif attempt_id not in cache:
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
    return bool(cache.get(attempt_id, False))


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

    Effect is resolved from ``coscientist.core.tool_registry`` by tool name.
    Unregistered names → ``external_effect``. Read calls skip the effect journal.
    Taint is read from ``AttemptStarted.tainted_context`` in the journal.
    """
    name = tool.spec.name
    reg = resolve_spec(name)
    effect = resolve_effect(name)  # authoritative; ignore tool.spec.effect
    digest = args_digest(args)

    # --- read: no gate, no EffectIntended/Done ---
    if effect == "read":
        return await tool.call(args, ctx)

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

    try:
        output = await tool.call(args, ctx)
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
    except Exception as exc:  # noqa: BLE001 — surface as opaque tool failure
        raise ToolError("opaque", f"tool raised: {type(exc).__name__}: {exc}") from exc

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
