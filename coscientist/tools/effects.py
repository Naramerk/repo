"""effect_idem_key + EffectIntended/EffectDone invoke path (DESIGN_M1 §8).

``invoke_tool`` is the **only** allowed path for tool calls that may have
side effects. Effect class comes from the closed core registry
(``resolve_effect``), never from a caller-supplied ``ToolSpec.effect``.
Unregistered tools are treated as ``external_effect`` (fail-closed).

Read tools are not journaled in the effect index (intentional repeats OK).
Write / external_effect keys include ``attempt_id`` so retries are distinct
from same-args repeats across attempts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.taint import bindings_tainted, context_tainted_from_ids
from coscientist.core.tool_registry import resolve_effect, resolve_spec
from coscientist.core.writer import StudyWriter
from coscientist.tools.contract import CallCtx, Tool, ToolError, ToolOutput
from coscientist.tools.gate import side_effect_allowed


def effect_idem_key(
    *,
    study_id: str,
    task_key: str,
    tool: str,
    args_digest: str,
    attempt_id: str,
) -> str:
    """sha256(JCS({study_id, task_key, tool, args_digest, attempt_id})).

    ``attempt_id`` is required so intentional re-attempts are not collapsed into
    a single cached write. Read tools are not indexed at all.
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


async def _load_index_from_writer(writer: StudyWriter) -> EffectIndex:
    store = getattr(writer, "store", None)
    if store is None or not hasattr(store, "read_all"):
        return EffectIndex()
    events = await store.read_all()
    return load_effect_index(events)


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


def _compute_tainted(ctx: CallCtx, artifacts: LocalArtifactStore | None) -> bool:
    """Core taint from input artifacts — ignores any caller-supplied flag."""
    get_meta = getattr(artifacts, "get_meta", None) if artifacts is not None else None
    if ctx.input_bindings:
        return bindings_tainted(ctx.input_bindings, get_meta)
    if ctx.input_artifact_ids:
        return context_tainted_from_ids(ctx.input_artifact_ids, get_meta)
    return False


async def _append(
    writer: StudyWriter,
    ev: EventEnvelope,
    on_event: Any | None,
) -> EventEnvelope:
    stored = await writer.append(ev)
    if on_event is not None:
        on_event(stored)
    return stored


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
    """
    name = tool.spec.name
    reg = resolve_spec(name)
    effect = resolve_effect(name)  # authoritative; ignore tool.spec.effect
    digest = args_digest(args)
    tainted = _compute_tainted(ctx, artifacts)
    ctx.tainted_context = tainted  # surface computed value for Tool.call

    # --- read: no gate, no EffectIntended/Done (do not cache forever) ---
    if effect == "read":
        return await tool.call(args, ctx)

    # --- write / external_effect ---
    if tainted:
        raise ToolError(
            "local",
            "tainted context: write/external_effect tools are not available",
        )
    if not side_effect_allowed(ctx.hitl_policy):
        deny_key = effect_idem_key(
            study_id=ctx.study_id,
            task_key=ctx.task_key,
            tool=name,
            args_digest=digest,
            attempt_id=ctx.attempt_id,
        )
        await _append(
            writer,
            EventEnvelope(
                study_id=ctx.study_id,
                type="SideEffectDenied",
                entity_key=deny_key,
                idem_key=make_idem("SideEffectDenied", deny_key),
                payload={
                    "key": deny_key,
                    "tool": name,
                    "task_key": ctx.task_key,
                    "attempt_id": ctx.attempt_id,
                    "effect": effect,
                    "reason": "side_effect_deny",
                },
                ts=datetime.now(UTC),
                schema_version=1,
            ),
            on_event,
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
    # Always load from the journal so an empty in-memory index cannot re-fire.
    idx = await _load_index_from_writer(writer)
    if effect_index is not None:
        # Overlay caller cache after journal (tests); journal wins on conflicts.
        for k, v in effect_index.done.items():
            idx.done.setdefault(k, v)
        for k, v in effect_index.intended.items():
            idx.intended.setdefault(k, v)

    done = idx.done.get(key)
    if done is not None:
        return _restore_output(artifacts, done)

    intended = idx.intended.get(key)
    if intended is not None and key not in idx.done:
        if not reg.supports_idem_key:
            raise ToolError(
                "opaque",
                "EffectIntended without EffectDone; status unknown "
                "(tool does not support idem_key)",
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
            },
            ts=now,
            schema_version=1,
        ),
        on_event,
    )
    idx.note_intended(key, {"key": key, "tool": name, "attempt_id": ctx.attempt_id})

    if reg.supports_idem_key:
        ctx.extra["effect_idem_key"] = key

    try:
        output = await tool.call(args, ctx)
    except ToolError as err:
        if err.kind == "transport_unsent":
            # Terminal Done(failed) so the key is not left opaque forever.
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

    result_ref: str | None = None
    if artifacts is not None:
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
    else:
        # Persist a minimal inline payload digest as result_ref substitute via CAS-less
        # path is not allowed for Done(ok) — require artifact store for success.
        raise ToolError(
            "local",
            "artifact store required to record EffectDone for write/external_effect",
        )

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
