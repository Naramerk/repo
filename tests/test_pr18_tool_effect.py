"""M1 PR18: ToolSpec.effect, side_effect gate, effect idempotency, taint, Idempotency-Key."""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from coscientist.api.studies import (
    CreateStudyBody,
    _idem_path,
    _request_digest,
    _write_owner_sidecar,
    post_studies,
    study_id_from_idempotency_key,
)
from coscientist.checks.catalog import minimal_test_catalog
from coscientist.core.events.errors import Conflict
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import Artifact, HitlPolicy
from coscientist.core.execution.taint import (
    current_attempt_taint,
    derived_tainted,
    push_attempt_taint,
    reset_attempt_taint,
)
from coscientist.core.execution.tool_registry import (
    RegisteredToolSpec,
    RegistryDuplicateError,
    RegistryFrozenError,
    RegistryUnauthorizedError,
    _register_tool_for_tests,
    _reset_registry_for_tests,
    freeze_registry,
    is_registry_frozen,
    register_tool,
    resolve_effect,
)
from coscientist.core.finalize.finalize import contributing_reasons_ordered
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
    _reset_registry_for_tests()
    for name, effect in (
        ("lookup", "read"),
        ("ping", "write"),
        ("mutate", "write"),
        ("get", "read"),
        ("put", "write"),
        ("email", "external_effect"),
        ("flaky", "write"),
        ("flaky_idem", "write"),
        ("net_write", "write"),
    ):
        _register_tool_for_tests(
            RegisteredToolSpec(
                name=name,
                effect=effect,  # type: ignore[arg-type]
                supports_idem_key=(name == "flaky_idem"),
            )
        )
    yield
    _reset_registry_for_tests()


def test_default_effect_read_advisory_only():
    spec = ToolSpec(name="lookup")
    assert spec.effect == "read"
    assert resolve_effect("lookup") == "read"
    assert resolve_effect("not_in_registry") == "external_effect"
    assert side_effect_decision(HitlPolicy()) == "deny"
    assert side_effect_allowed(HitlPolicy()) is False


def test_register_tool_rejects_non_loader_callers():
    """Production register_tool is loader-gated (pre-freeze spoof cannot stick)."""
    with pytest.raises(RegistryUnauthorizedError):
        register_tool(RegisteredToolSpec(name="spoof", effect="read"))


def test_registry_freeze_forbids_replace_and_spoof():
    freeze_registry()
    assert is_registry_frozen()
    from coscientist.core.execution import tool_registry as tr

    assert isinstance(tr._REGISTRY, MappingProxyType)
    with pytest.raises(RegistryFrozenError):
        register_tool(RegisteredToolSpec(name="mutate", effect="read"))
    with pytest.raises(RegistryFrozenError):
        _register_tool_for_tests(RegisteredToolSpec(name="brand_new", effect="read"))
    # Unfrozen duplicate also forbidden.
    _reset_registry_for_tests()
    _register_tool_for_tests(RegisteredToolSpec(name="x", effect="write"))
    with pytest.raises(RegistryDuplicateError):
        _register_tool_for_tests(RegisteredToolSpec(name="x", effect="read"))
    assert resolve_effect("x") == "write"


@pytest.mark.asyncio
async def test_register_tool_after_runtime_start_raises(tmp_path: Path):
    """open_runtime freezes via loader — late register_tool / test-register raise."""
    rt = await open_runtime(tmp_path, "st_freeze")
    try:
        assert is_registry_frozen()
        from coscientist.core.execution import tool_registry as tr

        assert isinstance(tr._REGISTRY, MappingProxyType)
        with pytest.raises(RegistryFrozenError):
            register_tool(RegisteredToolSpec(name="late_tool", effect="read"))
        with pytest.raises(RegistryFrozenError):
            _register_tool_for_tests(RegisteredToolSpec(name="late_test", effect="read"))
    finally:
        await close_runtime(rt)


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


async def _journal_attempt(
    rt: Any,
    *,
    attempt_id: str,
    task_key: str = "EXP-0",
    tainted: bool = False,
) -> None:
    sid = rt.study.study_id if rt.study else "st"
    await rt.writer.append(
        EventEnvelope(
            study_id=sid,
            type="AttemptStarted",
            entity_key=attempt_id,
            idem_key=make_idem("AttemptStarted", attempt_id),
            payload={
                "attempt_id": attempt_id,
                "n": 1,
                "task_id": f"{sid}/{task_key}",
                "study_id": sid,
                "provenance": "real",
                "deadline": datetime.now(UTC).isoformat(),
                "inputs": [],
                "tainted_context": tainted,
                "limitations": [],
            },
            ts=datetime.now(UTC),
            schema_version=1,
)
    )


@pytest.mark.asyncio
async def test_write_requires_gate(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path)
    arts = LocalArtifactStore(tmp_path / "a1")
    try:
        await _journal_attempt(rt, attempt_id="a#1")
        write = _CountingTool(ToolSpec(name="mutate", effect="read"))
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=HitlPolicy(),
        )
        with pytest.raises(ToolError) as ei:
            await invoke_tool(write, {"x": 1}, ctx, writer=rt.writer, artifacts=arts)
        assert ei.value.kind == "local"
        assert "side_effect" in ei.value.detail
        assert write.calls == []
        assert any(e.type == "SideEffectDenied" for e in await rt.store.read_all())

        arts2 = LocalArtifactStore(tmp_path / "a2")
        ctx_allow = CallCtx(
            study_id=ctx.study_id,
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=_allow_side_effect(),
        )
        out = await invoke_tool(
            write, {"x": 1}, ctx_allow, writer=rt.writer, artifacts=arts2
        )
        assert out.data["ok"] is True
        assert len(write.calls) == 1
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_unregistered_tool_is_external_effect(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_unk")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        await _journal_attempt(rt, attempt_id="a#1")
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
async def test_crash_new_attempt_does_not_repeat_effect(tmp_path: Path):
    """Intended-without-Done on attempt#1 blocks invoke on attempt#2 (exactly-once)."""
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_xonce")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        await _journal_attempt(rt, attempt_id="a#1")
        await _journal_attempt(rt, attempt_id="a#2")
        tool = _CountingTool(ToolSpec(name="ping", effect="write"))
        digest = args_digest({"q": "a"})
        # Simulate crash after Intended on attempt #1 (no Done).
        key1 = effect_idem_key(
            study_id=rt.study.study_id if rt.study else "st_pr18_xonce",
            task_key="EXP-0",
            tool="ping",
            args_digest=digest,
            attempt_id="a#1",
        )
        sid = rt.study.study_id if rt.study else "st_pr18_xonce"
        await rt.writer.append(
            EventEnvelope(
                study_id=sid,
                type="EffectIntended",
                entity_key=key1,
                idem_key=make_idem("EffectIntended", key1),
                payload={
                    "key": key1,
                    "study_id": sid,
                    "task_key": "EXP-0",
                    "tool": "ping",
                    "args_digest": digest,
                    "effect": "write",
                    "attempt_id": "a#1",
                },
                ts=datetime.now(UTC),
                schema_version=1,
)
        )
        # New attempt, different effect_idem_key — must still refuse.
        ctx2 = CallCtx(
            study_id=sid,
            task_key="EXP-0",
            attempt_id="a#2",
            hitl_policy=_allow_side_effect(),
        )
        with pytest.raises(ToolError) as ei:
            await invoke_tool(
                tool, {"q": "a"}, ctx2, writer=rt.writer, artifacts=arts
            )
        assert ei.value.kind == "opaque"
        assert "unknown" in ei.value.detail
        assert tool.calls == []
        lines = contributing_reasons_ordered(await rt.store.read_all())
        assert any(x.startswith("эффект неизвестен:") for x in lines)
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_effect_not_repeated_on_same_attempt_retry(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_idem")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        await _journal_attempt(rt, attempt_id="a#1")
        tool = _CountingTool(ToolSpec(name="ping", effect="write"))
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18_idem",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=_allow_side_effect(),
        )
        out1 = await invoke_tool(tool, {"q": "a"}, ctx, writer=rt.writer, artifacts=arts)
        out2 = await invoke_tool(tool, {"q": "a"}, ctx, writer=rt.writer, artifacts=arts)
        assert len(tool.calls) == 1
        assert out1.data["n"] == out2.data["n"] == 1
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
        assert len(tool.calls) == 2
        events = await rt.store.read_all()
        assert not any(e.type in ("EffectIntended", "EffectDone") for e in events)
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_transport_unsent_writes_done_failed(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_tunsent")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        await _journal_attempt(rt, attempt_id="a#1")
        tool = _CountingTool(
            ToolSpec(name="net_write", effect="write"), fail_kind="transport_unsent"
        )
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
        assert idx.done[key].get("status") == "failed"
        with pytest.raises(ToolError):
            await invoke_tool(tool, {}, ctx, writer=rt.writer, artifacts=arts)
        assert len(tool.calls) == 1
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_store_required_before_call(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_nostore")
    try:
        await _journal_attempt(rt, attempt_id="a#1")
        tool = _CountingTool(ToolSpec(name="ping", effect="write"))
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18_nostore",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=_allow_side_effect(),
        )
        with pytest.raises(ToolError) as ei:
            await invoke_tool(tool, {"q": "a"}, ctx, writer=rt.writer, artifacts=None)
        assert ei.value.kind == "local"
        assert "artifact store" in ei.value.detail
        assert tool.calls == []  # checked before call
    finally:
        await close_runtime(rt)


def test_tainted_context_no_write_tools():
    read = ToolSpec(name="get", effect="read")
    write = ToolSpec(name="put", effect="write")
    ext = ToolSpec(name="email", effect="external_effect")
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


@pytest.mark.asyncio
async def test_taint_from_journal_not_caller(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_taint")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        await _journal_attempt(rt, attempt_id="a#1", tainted=True)
        write = _CountingTool(ToolSpec(name="mutate", effect="write"))
        # Caller lies with tainted_context=False and empty input ids.
        ctx = CallCtx(
            study_id=rt.study.study_id if rt.study else "st_pr18_taint",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=_allow_side_effect(),
            tainted_context=False,
            input_artifact_ids=[],
        )
        with pytest.raises(ToolError) as ei:
            await invoke_tool(write, {}, ctx, writer=rt.writer, artifacts=arts)
        assert ei.value.kind == "local"
        assert "tainted" in ei.value.detail
        assert write.calls == []
        events = await rt.store.read_all()
        deny = next(e for e in events if e.type == "SideEffectDenied")
        assert deny.payload.get("reason") == "taint_deny"
        lines = contributing_reasons_ordered(events)
        assert any(x.startswith("отклонено: tainted контекст") for x in lines)
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_unknown_attempt_id_fail_closed_taint(tmp_path: Path):
    """Missing AttemptStarted for attempt_id → treat as tainted (deny writes)."""
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_unknown")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        write = _CountingTool(ToolSpec(name="mutate", effect="write"))
        ctx = CallCtx(
            study_id="st_pr18_unknown",
            task_key="EXP-0",
            attempt_id="never-started#1",
            hitl_policy=_allow_side_effect(),
            tainted_context=False,
        )
        with pytest.raises(ToolError) as ei:
            await invoke_tool(write, {}, ctx, writer=rt.writer, artifacts=arts)
        assert ei.value.kind == "local"
        assert "tainted" in ei.value.detail
        assert write.calls == []
        deny = next(e for e in await rt.store.read_all() if e.type == "SideEffectDenied")
        assert deny.payload.get("reason") == "taint_deny"
    finally:
        await close_runtime(rt)


def test_attempt_taint_contextvar_isolation():
    """Nested ContextVar tokens isolate taint (not shared store state)."""
    token_a = push_attempt_taint(True)
    try:
        assert current_attempt_taint() is True
        assert derived_tainted(context_tainted=False) is True
        token_b = push_attempt_taint(False)
        try:
            assert current_attempt_taint() is False
            assert derived_tainted(context_tainted=False) is False
        finally:
            reset_attempt_taint(token_b)
        assert current_attempt_taint() is True
    finally:
        reset_attempt_taint(token_a)


@pytest.mark.asyncio
async def test_parallel_attempt_taint_puts(tmp_path: Path):
    arts = LocalArtifactStore(tmp_path / "arts")

    async def put_with(tainted: bool, name: str) -> bool:
        tok = push_attempt_taint(tainted)
        try:
            # Yield so both contexts interleave.
            await asyncio.sleep(0)
            art = arts.put(
                name.encode(),
                make_tool_ref(f"c:{name}"),
                name=name,
                media_type="text/plain",
            )
            await asyncio.sleep(0)
            return art.tainted
        finally:
            reset_attempt_taint(tok)

    results = await asyncio.gather(
        put_with(True, "infected"),
        put_with(False, "clean"),
        put_with(True, "infected2"),
        put_with(False, "clean2"),
    )
    assert results == [True, False, True, False]


@pytest.mark.asyncio
async def test_side_effect_deny_in_report(tmp_path: Path):
    rt = await _runtime_with_writer(tmp_path, sid="st_pr18_deny_rpt")
    arts = LocalArtifactStore(tmp_path / "arts")
    try:
        await _journal_attempt(rt, attempt_id="a#1")
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


@pytest.mark.asyncio
async def test_post_studies_empty_reservation_recoverable(tmp_path: Path):
    """Crash between O_EXCL and write leaves empty file — must not permanent Conflict."""
    catalog = minimal_test_catalog()
    body = CreateStudyBody(request="empty reserve", profile="research")
    key = "empty-key"
    path = _idem_path(Path(tmp_path), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")  # empty reservation
    r = await post_studies(tmp_path, body, idempotency_key=key, catalog=catalog)
    assert r["study_id"] == study_id_from_idempotency_key(key)
    assert json.loads(path.read_text())["status"] == "complete"


@pytest.mark.asyncio
async def test_post_studies_recent_corrupt_reservation_conflict(tmp_path: Path):
    """Fresh non-empty unreadable file may be a live neighbour — Conflict, do not unlink."""
    catalog = minimal_test_catalog()
    body = CreateStudyBody(request="live neighbour", profile="research")
    key = "live-corrupt-key"
    path = _idem_path(Path(tmp_path), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not-json-yet")
    _write_owner_sidecar(path, hostname=socket.gethostname(), owner_pid=os.getpid())
    os.utime(path, (time.time(), time.time()))
    with pytest.raises(Conflict, match="not yet stale"):
        await post_studies(tmp_path, body, idempotency_key=key, catalog=catalog)
    assert path.exists() and path.read_bytes().startswith(b"{not-json")


@pytest.mark.asyncio
async def test_post_studies_stale_corrupt_reservation_reclaimed(tmp_path: Path):
    """Old unreadable reservation is reclaimable (crash mid-write, owner gone)."""
    catalog = minimal_test_catalog()
    body = CreateStudyBody(request="stale corrupt", profile="research")
    key = "stale-corrupt-key"
    path = _idem_path(Path(tmp_path), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{truncated")
    old = time.time() - 120.0
    os.utime(path, (old, old))
    r = await post_studies(tmp_path, body, idempotency_key=key, catalog=catalog)
    assert r["study_id"] == study_id_from_idempotency_key(key)
    assert json.loads(path.read_text())["status"] == "complete"


@pytest.mark.asyncio
async def test_post_studies_dead_owner_sidecar_reclaimed(tmp_path: Path):
    """Corrupt body + same-host dead pid in .owner sidecar → reclaim (pid branch)."""
    catalog = minimal_test_catalog()
    body = CreateStudyBody(request="dead owner sidecar", profile="research")
    key = "dead-owner-key"
    path = _idem_path(Path(tmp_path), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{partial")
    _write_owner_sidecar(path, hostname=socket.gethostname(), owner_pid=2_147_483_646)
    os.utime(path, (time.time(), time.time()))  # recent — age alone would not reclaim
    r = await post_studies(tmp_path, body, idempotency_key=key, catalog=catalog)
    assert r["study_id"] == study_id_from_idempotency_key(key)
    assert json.loads(path.read_text())["status"] == "complete"


@pytest.mark.asyncio
async def test_post_studies_foreign_hostname_no_pid_reclaim(tmp_path: Path):
    """Different hostname: never reclaim by pid — only by age."""
    catalog = minimal_test_catalog()
    body = CreateStudyBody(request="foreign host", profile="research")
    key = "foreign-host-key"
    path = _idem_path(Path(tmp_path), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{partial")
    _write_owner_sidecar(path, hostname="other-host.example", owner_pid=2_147_483_646)
    os.utime(path, (time.time(), time.time()))
    with pytest.raises(Conflict, match="not yet stale"):
        await post_studies(tmp_path, body, idempotency_key=key, catalog=catalog)
    assert path.exists()


@pytest.mark.asyncio
async def test_post_studies_restart_after_pending(tmp_path: Path):
    from coscientist.api.studies import _atomic_write

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
    r = await post_studies(
        tmp_path, body, idempotency_key=key, catalog=catalog, wait_complete_s=0.2
    )
    assert r["study_id"] == sid
    assert json.loads(path.read_text())["status"] == "complete"


@pytest.mark.asyncio
async def test_post_studies_takeover_after_wait_timeout(tmp_path: Path):
    """After wait_complete_s expires on stale pending, caller takes over create."""
    from coscientist.api.studies import _atomic_write

    catalog = minimal_test_catalog()
    body = CreateStudyBody(request="takeover after wait", profile="research")
    key = "takeover-key"
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
    # Short wait → timeout → takeover create (StudyCreated idempotent on retry).
    r = await post_studies(
        tmp_path, body, idempotency_key=key, catalog=catalog, wait_complete_s=0.1
    )
    assert r["study_id"] == sid
    assert json.loads(path.read_text())["status"] == "complete"


@pytest.mark.asyncio
async def test_post_studies_loser_sees_complete_during_wait(tmp_path: Path):
    """Concurrent loser waiting for complete observes winner finish (no double create)."""
    catalog = minimal_test_catalog()
    body = CreateStudyBody(request="wait sees complete", profile="research")
    key = "wait-complete-key"
    expected = study_id_from_idempotency_key(key)

    async def winner() -> str:
        r = await post_studies(
            tmp_path, body, idempotency_key=key, catalog=catalog, wait_complete_s=5.0
        )
        return r["study_id"]

    async def loser() -> str:
        await asyncio.sleep(0.05)
        r = await post_studies(
            tmp_path, body, idempotency_key=key, catalog=catalog, wait_complete_s=5.0
        )
        return r["study_id"]

    results = await asyncio.gather(winner(), loser())
    assert set(results) == {expected}
    studies = [
        p for p in Path(tmp_path).iterdir() if p.is_dir() and p.name.startswith("st_")
    ]
    assert len(studies) == 1


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


def _mp_post(data_root: str, key: str) -> str:
    """Module-level worker for multiprocess race (must be picklable)."""
    import asyncio as _aio

    from coscientist.api.studies import CreateStudyBody, post_studies
    from coscientist.checks.catalog import minimal_test_catalog

    async def _run() -> str:
        r = await post_studies(
            data_root,
            CreateStudyBody(request="mp race", profile="research"),
            idempotency_key=key,
            catalog=minimal_test_catalog(),
        )
        return r["study_id"]

    return _aio.run(_run())


def test_post_studies_multiprocess_race(tmp_path: Path):
    key = "mp-race-key"
    expected = study_id_from_idempotency_key(key)
    root = str(tmp_path)
    with mp.Pool(3) as pool:
        results = pool.starmap(_mp_post, [(root, key)] * 3)
    assert set(results) == {expected}
