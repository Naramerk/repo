"""M1 PR18: ToolSpec.effect, side_effect gate, effect idempotency, taint, Idempotency-Key."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from coscientist.api.studies import (
    CreateStudyBody,
    post_studies,
    study_id_from_idempotency_key,
)
from coscientist.checks.catalog import minimal_test_catalog
from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.domain import Artifact, HitlPolicy
from coscientist.core.errors import Conflict
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.finalize import contributing_reasons_ordered
from coscientist.core.taint import bindings_tainted, derived_tainted
from coscientist.core.tool_registry import (
    RegisteredToolSpec,
    clear_registry,
    register_tool,
    resolve_effect,
)
from coscientist.tools.contract import CallCtx, ToolError, ToolOutput, ToolSpec
from coscientist.tools.effects import (
    args_digest,
    effect_idem_key,
    invoke_tool,
    load_effect_index,
)
from coscientist.tools.gate import side_effect_allowed, side_effect_decision
from coscientist.tools.taint import context_is_tainted, filter_tools_for_context
from tests.helpers import close_runtime, open_runtime


@pytest.fixture(autouse=True)
def _registry():
    clear_registry()
    register_tool(RegisteredToolSpec(name="lookup", effect="read"))
    register_tool(RegisteredToolSpec(name="ping", effect="write"))
    register_tool(RegisteredToolSpec(name="mutate", effect="write"))
    register_tool(RegisteredToolSpec(name="get", effect="read"))
    register_tool(RegisteredToolSpec(name="put", effect="write"))
    register_tool(RegisteredToolSpec(name="email", effect="external_effect"))
    register_tool(
        RegisteredToolSpec(name="flaky", effect="write", supports_idem_key=False)
    )
    register_tool(
        RegisteredToolSpec(name="flaky_idem", effect="write", supports_idem_key=True)
    )
    register_tool(RegisteredToolSpec(name="net_write", effect="write"))
    yield
    clear_registry()


def test_default_effect_read_advisory_only():
    """ToolSpec.effect defaults to read, but registry is authoritative."""
    spec = ToolSpec(name="lookup")
    assert spec.effect == "read"
    assert resolve_effect("lookup") == "read"
    # Unregistered → external_effect (fail-closed), even if ToolSpec says read.
    assert resolve_effect("not_in_registry") == "external_effect"
    assert side_effect_decision(HitlPolicy()) == "deny"
    assert side_effect_allowed(HitlPolicy()) is False


@dataclass
class _CountingTool:
    spec: ToolSpec
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_kind: str | None = None

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
        self.calls.append({"args": dict(args), "extra": dict(ctx.extra)})
        if self.fail_kind:
            raise ToolError(self.fail_kind, f"forced {self.fail_kind}")  # type: ignore[arg-type]
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
        # Claiming effect=read on ToolSpec does NOT bypass — registry says write.
        write = _CountingTool(ToolSpec(name="mutate", effect="read"))
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=HitlPolicy(),
        )
        with pytest.raises(ToolError) as ei:
            await invoke_tool(write, {"x": 1}, ctx, writer=rt.writer, artifacts=LocalArtifactStore(tmp_path / "a1"))
        assert ei.value.kind == "local"
        assert "side_effect" in ei.value.detail
        assert write.calls == []
        events = await rt.store.read_all()
        assert any(e.type == "SideEffectDenied" for e in events)

        arts = LocalArtifactStore(tmp_path / "a2")
        ctx_allow = CallCtx(
            study_id=ctx.study_id,
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=_allow_side_effect(),
        )
        out = await invoke_tool(write, {"x": 1}, ctx_allow, writer=rt.writer, artifacts=arts)
        assert out.data["ok"] is True
        assert len(write.calls) == 1
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_unregistered_tool_is_external_effect(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_unk")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        # ToolSpec claims read — registry has no entry → external_effect → gate.
        evil = _CountingTool(ToolSpec(name="spoof_read", effect="read"))
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18_unk",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=HitlPolicy(),
        )
        with pytest.raises(ToolError) as ei:
            await invoke_tool(evil, {}, ctx, writer=rt.writer, artifacts=arts)
        assert ei.value.kind == "local"
        assert evil.calls == []
        assert resolve_effect("spoof_read") == "external_effect"
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_effect_not_repeated_on_retry(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_idem")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        tool = _CountingTool(ToolSpec(name="ping", effect="write"))
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18_idem",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=_allow_side_effect(),
        )
        out1 = await invoke_tool(tool, {"q": "a"}, ctx, writer=rt.writer, artifacts=arts)
        assert len(tool.calls) == 1
        # Empty in-memory index must still load from journal (no re-fire).
        out2 = await invoke_tool(tool, {"q": "a"}, ctx, writer=rt.writer, artifacts=arts)
        assert len(tool.calls) == 1
        assert out2.data["ok"] is True
        assert out1.data["n"] == out2.data["n"] == 1

        events = await rt.store.read_all()
        idx2 = load_effect_index(events)
        key = effect_idem_key(
            study_id=ctx.study_id,
            task_key=ctx.task_key,
            tool="ping",
            args_digest=args_digest({"q": "a"}),
            attempt_id=ctx.attempt_id,
        )
        assert key in idx2.done

        # Intended without Done + no supports_idem_key → opaque (unknown).
        flaky = _CountingTool(ToolSpec(name="flaky", effect="write"))
        dctx = CallCtx(
            study_id=ctx.study_id,
            task_key="EXP-1",
            attempt_id="a#2",
            hitl_policy=_allow_side_effect(),
        )
        dkey = effect_idem_key(
            study_id=dctx.study_id,
            task_key=dctx.task_key,
            tool="flaky",
            args_digest=args_digest({}),
            attempt_id=dctx.attempt_id,
        )
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
                    "effect": "write",
                    "attempt_id": dctx.attempt_id,
                },
                ts=datetime.now(UTC),
                schema_version=1,
            )
        )
        with pytest.raises(ToolError) as ei:
            await invoke_tool(flaky, {}, dctx, writer=rt.writer, artifacts=arts)
        assert ei.value.kind == "opaque"
        assert "unknown" in ei.value.detail
        assert flaky.calls == []

        # Unknown effect appears in report contributing reasons.
        lines = contributing_reasons_ordered(await rt.store.read_all())
        assert any(x.startswith("эффект неизвестен:") for x in lines)
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_read_not_indexed(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_read")
    try:
        tool = _CountingTool(ToolSpec(name="lookup", effect="read"))
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18_read",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=HitlPolicy(),
        )
        await invoke_tool(tool, {"q": 1}, ctx, writer=rt.writer)
        await invoke_tool(tool, {"q": 1}, ctx, writer=rt.writer)
        assert len(tool.calls) == 2  # intentional repeats
        events = await rt.store.read_all()
        assert not any(e.type in ("EffectIntended", "EffectDone") for e in events)
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_transport_unsent_writes_done_failed(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_tunsent")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        tool = _CountingTool(ToolSpec(name="net_write", effect="write"), fail_kind="transport_unsent")
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18_tunsent",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=_allow_side_effect(),
        )
        with pytest.raises(ToolError) as ei:
            await invoke_tool(tool, {}, ctx, writer=rt.writer, artifacts=arts)
        assert ei.value.kind == "transport_unsent"
        idx = load_effect_index(await rt.store.read_all())
        key = effect_idem_key(
            study_id=ctx.study_id,
            task_key=ctx.task_key,
            tool="net_write",
            args_digest=args_digest({}),
            attempt_id=ctx.attempt_id,
        )
        assert key in idx.done
        assert idx.done[key].get("status") == "failed"
        # Retry restores Done(failed) → ToolError, no second call.
        with pytest.raises(ToolError) as ei2:
            await invoke_tool(tool, {}, ctx, writer=rt.writer, artifacts=arts)
        assert ei2.value.kind == "transport_unsent"
        assert len(tool.calls) == 1
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_restore_without_artifacts_errors(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_nostore")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        tool = _CountingTool(ToolSpec(name="ping", effect="write"))
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18_nostore",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=_allow_side_effect(),
        )
        await invoke_tool(tool, {"q": "a"}, ctx, writer=rt.writer, artifacts=arts)
        with pytest.raises(ToolError) as ei:
            await invoke_tool(tool, {"q": "a"}, ctx, writer=rt.writer, artifacts=None)
        assert ei.value.kind == "opaque"
        assert "artifact store" in ei.value.detail
        assert "restored" not in ei.value.detail
    finally:
        await close_runtime(rt)


def test_tainted_context_no_write_tools():
    register_tool(RegisteredToolSpec(name="get", effect="read"))
    register_tool(RegisteredToolSpec(name="put", effect="write"))
    register_tool(RegisteredToolSpec(name="email", effect="external_effect"))
    read = ToolSpec(name="get", effect="read")
    write = ToolSpec(name="put", effect="write")
    ext = ToolSpec(name="email", effect="external_effect")
    # Unregistered "fake_read" claiming read is still external_effect → filtered.
    fake = ToolSpec(name="fake_read", effect="read")
    filtered = filter_tools_for_context(
        [read, write, ext, fake], tainted_context=True
    )
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
    assert bindings_tainted(
        [{"artifact_id": "art_x"}],
        lambda aid: art if aid == "art_x" else None,
    )


@pytest.mark.asyncio
async def test_taint_computed_from_inputs_not_caller(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_taint")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        tainted = arts.put_tool(
            b"web",
            make_tool_ref("seed:1", tool="web"),
            name="page",
            media_type="text/html",
        )
        assert tainted.tainted is True
        # Re-put with context_tainted must keep the flag (propagation).
        derived = arts.put(
            b"derived",
            make_tool_ref("seed:2"),
            name="child",
            media_type="text/plain",
            context_tainted=True,
        )
        assert derived.tainted is True
        assert derived_tainted(context_tainted=True) is True

        write = _CountingTool(ToolSpec(name="mutate", effect="write"))
        # Caller lies with tainted_context=False — core still sees the input art.
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18_taint",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=_allow_side_effect(),
            tainted_context=False,
            input_artifact_ids=[tainted.artifact_id],
        )
        with pytest.raises(ToolError) as ei:
            await invoke_tool(write, {}, ctx, writer=rt.writer, artifacts=arts)
        assert ei.value.kind == "local"
        assert "tainted" in ei.value.detail
        assert write.calls == []
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_side_effect_deny_in_report(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_deny_rpt")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        write = _CountingTool(ToolSpec(name="mutate", effect="write"))
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18_deny_rpt",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=HitlPolicy(),
        )
        with pytest.raises(ToolError):
            await invoke_tool(write, {}, ctx, writer=rt.writer, artifacts=arts)
        lines = contributing_reasons_ordered(await rt.store.read_all())
        assert any(x.startswith("отклонено: побочный эффект") for x in lines)
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_post_studies_idempotent(tmp_path: Path):
    catalog = minimal_test_catalog()
    body = CreateStudyBody(request="idempotent create", profile="research")
    key = "client-key-001"
    expected = study_id_from_idempotency_key(key)
    r1 = await post_studies(tmp_path, body, idempotency_key=key, catalog=catalog)
    r2 = await post_studies(tmp_path, body, idempotency_key=key, catalog=catalog)
    assert r1["study_id"] == r2["study_id"] == expected

    other = CreateStudyBody(request="different body", profile="research")
    with pytest.raises(Conflict):
        await post_studies(tmp_path, other, idempotency_key=key, catalog=catalog)

    r3 = await post_studies(
        tmp_path, body, idempotency_key="client-key-002", catalog=catalog
    )
    assert r3["study_id"] != r1["study_id"]


@pytest.mark.asyncio
async def test_post_studies_restart_after_pending(tmp_path: Path):
    """Crash after O_EXCL reserve / before complete → retry returns same study."""
    import json

    from coscientist.api.studies import _atomic_write, _idem_path, _request_digest

    catalog = minimal_test_catalog()
    body = CreateStudyBody(request="restart safe", profile="research")
    key = "restart-key"
    sid = study_id_from_idempotency_key(key)
    path = _idem_path(Path(tmp_path), key)
    _atomic_write(
        path,
        {
            "study_id": sid,
            "request_digest": _request_digest(body),
            "idempotency_key": key,
            "status": "pending",
        },
    )
    r = await post_studies(tmp_path, body, idempotency_key=key, catalog=catalog)
    assert r["study_id"] == sid
    mapping = json.loads(path.read_text())
    assert mapping["status"] == "complete"


@pytest.mark.asyncio
async def test_post_studies_concurrent_race(tmp_path: Path):
    catalog = minimal_test_catalog()
    body = CreateStudyBody(request="concurrent", profile="research")
    key = "race-key"
    expected = study_id_from_idempotency_key(key)

    async def one() -> str:
        r = await post_studies(tmp_path, body, idempotency_key=key, catalog=catalog)
        return r["study_id"]

    results = await asyncio.gather(one(), one(), one())
    assert set(results) == {expected}
    # Only one study directory under data_root.
    studies = [p for p in Path(tmp_path).iterdir() if p.is_dir() and p.name.startswith("st_")]
    assert len(studies) == 1


def test_effect_idem_key_includes_attempt():
    a = effect_idem_key(
        study_id="s", task_key="T", tool="t", args_digest="d", attempt_id="a#1"
    )
    b = effect_idem_key(
        study_id="s", task_key="T", tool="t", args_digest="d", attempt_id="a#2"
    )
    assert a != b
