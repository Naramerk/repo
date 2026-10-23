"""M1 PR18: ToolSpec.effect, side_effect gate, effect idempotency, taint, Idempotency-Key."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from coscientist.api.studies import CreateStudyBody, post_studies
from coscientist.checks.catalog import minimal_test_catalog
from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.domain import Artifact, HitlPolicy
from coscientist.core.errors import Conflict
from coscientist.core.events import make_idem
from coscientist.tools.contract import CallCtx, ToolError, ToolOutput, ToolSpec
from coscientist.tools.effects import (
    EffectIndex,
    args_digest,
    effect_idem_key,
    invoke_tool,
    load_effect_index,
)
from coscientist.tools.gate import side_effect_allowed, side_effect_decision
from coscientist.tools.taint import context_is_tainted, filter_tools_for_context
from tests.helpers import close_runtime, open_runtime


def test_default_effect_read():
    spec = ToolSpec(name="lookup")
    assert spec.effect == "read"
    assert spec.idempotent is False
    assert side_effect_decision(HitlPolicy()) == "deny"
    assert side_effect_allowed(HitlPolicy()) is False


@dataclass
class _CountingTool:
    spec: ToolSpec
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
        self.calls.append({"args": dict(args), "extra": dict(ctx.extra)})
        return ToolOutput(data={"ok": True, "n": len(self.calls), **args})


def _allow_side_effect() -> HitlPolicy:
    return HitlPolicy(defaults={"side_effect": "allow"})


async def _runtime_with_writer(tmp_path: Path, sid: str = "st_pr18"):
    rt = await open_runtime(tmp_path, sid)
    await rt.create_study(
        profile="research",
        request="tool effect study",
        catalog_version="test",
        sid=sid,
        finalize_reserve_s=5,
    )
    return rt


@pytest.mark.asyncio
async def test_write_requires_gate(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path)
    try:
        write = _CountingTool(ToolSpec(name="mutate", effect="write"))
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=HitlPolicy(),  # headless default deny
        )
        with pytest.raises(ToolError) as ei:
            await invoke_tool(write, {"x": 1}, ctx, writer=rt.writer)
        assert ei.value.kind == "local"
        assert "side_effect" in ei.value.detail
        assert write.calls == []

        ctx_allow = CallCtx(
            study_id=ctx.study_id,
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=_allow_side_effect(),
        )
        out = await invoke_tool(write, {"x": 1}, ctx_allow, writer=rt.writer)
        assert out.data["ok"] is True
        assert len(write.calls) == 1
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_effect_not_repeated_on_retry(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_idem")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        tool = _CountingTool(ToolSpec(name="ping", effect="read"))
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18_idem",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=HitlPolicy(),
        )
        idx = EffectIndex()
        out1 = await invoke_tool(
            tool, {"q": "a"}, ctx, writer=rt.writer, artifacts=arts, effect_index=idx
        )
        assert len(tool.calls) == 1
        out2 = await invoke_tool(
            tool, {"q": "a"}, ctx, writer=rt.writer, artifacts=arts, effect_index=idx
        )
        assert len(tool.calls) == 1  # Done short-circuit
        assert out2.data["ok"] is True
        assert out1.data["n"] == out2.data["n"] == 1

        # Recover path: rebuild index from journal → still no re-call.
        events = await rt.store.read_all()
        idx2 = load_effect_index(events)
        key = effect_idem_key(
            study_id=ctx.study_id,
            task_key=ctx.task_key,
            tool="ping",
            args_digest=args_digest({"q": "a"}),
        )
        assert key in idx2.done
        out3 = await invoke_tool(
            tool, {"q": "a"}, ctx, writer=rt.writer, artifacts=arts, effect_index=idx2
        )
        assert len(tool.calls) == 1
        assert out3.data["ok"] is True

        # Intended without Done + no supports_idem_key → opaque.
        dangling = ToolSpec(name="flaky", effect="read", supports_idem_key=False)
        flaky = _CountingTool(dangling)
        dctx = CallCtx(
            study_id=ctx.study_id,
            task_key="EXP-1",
            attempt_id="a#2",
            hitl_policy=HitlPolicy(),
        )
        dkey = effect_idem_key(
            study_id=dctx.study_id,
            task_key=dctx.task_key,
            tool="flaky",
            args_digest=args_digest({}),
        )
        from datetime import UTC, datetime

        from coscientist.core.events import EventEnvelope

        await rt.writer.append(
            EventEnvelope(
                study_id=dctx.study_id,
                type="EffectIntended",
                entity_key=dkey,
                idem_key=make_idem("EffectIntended", dkey),
                payload={
                    "key": dkey,
                    "study_id": dctx.study_id,
                    "task_key": dctx.task_key,
                    "tool": "flaky",
                    "args_digest": args_digest({}),
                    "effect": "read",
                    "attempt_id": dctx.attempt_id,
                },
                ts=datetime.now(UTC),
                schema_version=1,
            )
        )
        idx3 = load_effect_index(await rt.store.read_all())
        with pytest.raises(ToolError) as ei:
            await invoke_tool(
                flaky, {}, dctx, writer=rt.writer, artifacts=arts, effect_index=idx3
            )
        assert ei.value.kind == "opaque"
        assert flaky.calls == []
    finally:
        await close_runtime(rt)


def test_tainted_context_no_write_tools():
    read = ToolSpec(name="get", effect="read")
    write = ToolSpec(name="put", effect="write")
    ext = ToolSpec(name="email", effect="external_effect")
    clean = filter_tools_for_context([read, write, ext], tainted_context=False)
    assert [t.name for t in clean] == ["get", "put", "email"]
    filtered = filter_tools_for_context([read, write, ext], tainted_context=True)
    assert [t.name for t in filtered] == ["get"]

    art = Artifact(
        artifact_id="art_x",
        name="web",
        media_type="text/html",
        sha256="0" * 64,
        size=0,
        uri="cas://x",
        produced_by=make_tool_ref("a:1", tool="web"),
        tainted=True,
    )
    assert context_is_tainted([art]) is True
    assert context_is_tainted([art.model_copy(update={"tainted": False})]) is False


@pytest.mark.asyncio
async def test_tainted_context_blocks_invoke(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_taint")
    try:
        write = _CountingTool(ToolSpec(name="mutate", effect="write"))
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18_taint",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=_allow_side_effect(),
            tainted_context=True,
        )
        with pytest.raises(ToolError) as ei:
            await invoke_tool(write, {}, ctx, writer=rt.writer)
        assert ei.value.kind == "local"
        assert "tainted" in ei.value.detail
        assert write.calls == []
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_post_studies_idempotent(tmp_path: Path):
    catalog = minimal_test_catalog()
    body = CreateStudyBody(request="idempotent create", profile="research")
    key = "client-key-001"
    r1 = await post_studies(tmp_path, body, idempotency_key=key, catalog=catalog)
    r2 = await post_studies(tmp_path, body, idempotency_key=key, catalog=catalog)
    assert r1["study_id"] == r2["study_id"]

    other = CreateStudyBody(request="different body", profile="research")
    with pytest.raises(Conflict):
        await post_studies(tmp_path, other, idempotency_key=key, catalog=catalog)

    # Distinct keys → distinct studies.
    r3 = await post_studies(
        tmp_path, body, idempotency_key="client-key-002", catalog=catalog
    )
    assert r3["study_id"] != r1["study_id"]
