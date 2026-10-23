"""effect_idem_key + EffectIntended/EffectDone invoke path (DESIGN_M1 §8)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.writer import StudyWriter
from coscientist.tools.contract import CallCtx, Tool, ToolError, ToolOutput
from coscientist.tools.gate import side_effect_allowed


def effect_idem_key(
    *,
    study_id: str,
    task_key: str,
    tool: str,
    args_digest: str,
) -> str:
    """sha256(JCS({study_id, task_key, tool, args_digest}))."""
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


def _restore_output(
    arts: LocalArtifactStore | None,
    result_ref: str | None,
) -> ToolOutput:
    if not result_ref or arts is None:
        return ToolOutput(data={"restored": True, "result_ref": result_ref})
    raw = arts.read(result_ref)
    payload = json.loads(raw.decode("utf-8"))
    return ToolOutput.model_validate(payload.get("output") or payload)


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
    """Call a tool under the side_effect gate + effect idempotency protocol.

    - ``effect=read``: no gate; still journals Intended/Done when writer is used.
    - ``write`` / ``external_effect``: require ``side_effect`` allow; tainted
      context refuses the call (tools should already be filtered out).
    - On retry: ``EffectDone`` → return stored result; ``EffectIntended`` without
      Done → re-call only if ``supports_idem_key``, else ``ToolError(opaque)``.
    """
    spec = tool.spec
    digest = args_digest(args)
    key = effect_idem_key(
        study_id=ctx.study_id,
        task_key=ctx.task_key,
        tool=spec.name,
        args_digest=digest,
    )
    idx = effect_index if effect_index is not None else EffectIndex()

    if spec.effect != "read":
        if ctx.tainted_context:
            raise ToolError(
                "local",
                "tainted context: write/external_effect tools are not available",
            )
        if not side_effect_allowed(ctx.hitl_policy):
            raise ToolError(
                "local",
                "side_effect gate denied (headless default deny)",
            )

    done = idx.done.get(key)
    if done is not None:
        return _restore_output(artifacts, done.get("result_ref"))

    intended = idx.intended.get(key)
    if intended is not None and key not in idx.done:
        if not spec.supports_idem_key:
            raise ToolError(
                "opaque",
                "EffectIntended without EffectDone; tool does not support idem_key",
            )

    now = datetime.now(UTC)
    intended_ev = EventEnvelope(
        study_id=ctx.study_id,
        type="EffectIntended",
        entity_key=key,
        idem_key=make_idem("EffectIntended", key),
        payload={
            "key": key,
            "study_id": ctx.study_id,
            "task_key": ctx.task_key,
            "tool": spec.name,
            "args_digest": digest,
            "effect": spec.effect,
            "attempt_id": ctx.attempt_id,
        },
        ts=now,
        schema_version=1,
    )
    stored = await writer.append(intended_ev)
    if on_event is not None:
        on_event(stored)
    idx.note_intended(key, intended_ev.payload)

    # Tools that opt in receive the key via CallCtx.extra.
    if spec.supports_idem_key:
        ctx.extra["effect_idem_key"] = key

    try:
        output = await tool.call(args, ctx)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface as opaque tool failure
        raise ToolError("opaque", f"tool raised: {type(exc).__name__}: {exc}") from exc

    # Tool-produced artifacts are tainted (DESIGN_M1 §8).
    if output.artifacts:
        tainted_arts = [
            a if a.tainted else a.model_copy(update={"tainted": True})
            for a in output.artifacts
        ]
        output = output.model_copy(update={"artifacts": tainted_arts})

    result_ref: str | None = None
    if artifacts is not None:
        blob = canonical_json(
            {"output": output.model_dump(mode="json"), "key": key}
        )
        ref = make_tool_ref(
            f"{ctx.attempt_id}:{ctx.call_seq}",
            tool=spec.name,
            args=digest,
        )
        art = artifacts.put_tool(
            blob,
            ref,
            name=f"effect:{spec.name}",
            media_type="application/json",
        )
        result_ref = art.artifact_id

    done_ev = EventEnvelope(
        study_id=ctx.study_id,
        type="EffectDone",
        entity_key=key,
        idem_key=make_idem("EffectDone", key),
        payload={
            "key": key,
            "result_ref": result_ref,
            "tool": spec.name,
            "attempt_id": ctx.attempt_id,
        },
        ts=datetime.now(UTC),
        schema_version=1,
    )
    stored_done = await writer.append(done_ev)
    if on_event is not None:
        on_event(stored_done)
    idx.note_done(key, done_ev.payload)
    return output
