"""Resolve → registry effect/is_fetch → invoke_tool (fail-closed)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import HitlPolicy, parse_provenance
from coscientist.core.execution.tool_registry import (
    RegisteredToolSpec,
    _register_tool_for_tests,
    _reset_registry_for_tests,
    freeze_registry,
    resolve_is_fetch,
)
from coscientist.tools.contract import CallCtx, ToolOutput, ToolSpec
from coscientist.tools.mcp_catalog import McpCatalogTool, McpToolCatalog
from coscientist.tools.resolve import (
    CatalogInvokeRefuse,
    ToolNeed,
    invoke_after_catalog_resolve,
)


def _catalog_tool(name: str) -> McpCatalogTool:
    return McpCatalogTool(
        name=name,
        description="d",
        input_schema={"type": "object"},
        provider="mcp",
    )


@dataclass
class _CountingTool:
    spec: ToolSpec
    calls: list[dict[str, object]] = field(default_factory=list)

    async def call(self, args: dict[str, object], ctx: CallCtx) -> ToolOutput:
        self.calls.append(dict(args))
        return ToolOutput(data={"ok": True})


@dataclass
class _FetchBodyTool:
    spec: ToolSpec
    arts: LocalArtifactStore
    body: bytes

    async def call(self, args: dict[str, object], ctx: CallCtx) -> ToolOutput:
        assert ctx.fetch_capability is not None
        call_id = f"{ctx.attempt_id}:{ctx.call_seq}"
        art = self.arts.put_fetched(
            self.body,
            make_tool_ref(call_id, tool=self.spec.name),
            name="paper.txt",
            media_type="text/plain",
            role="paper",
            capability=ctx.fetch_capability,
            expected_tool=self.spec.name,
            expected_call_id=call_id,
            expected_study_id=ctx.study_id,
            expected_attempt_id=ctx.attempt_id,
        )
        return ToolOutput(data={"url": "https://normalized.example/p"}, artifacts=[art])


async def _writer_store(tmp_path: Path) -> tuple[FakeClock, SqliteEventStore, StudyWriter]:
    path = tmp_path / "j.db"
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    store = SqliteEventStore("st_inv", path, clock=clock)
    await store.acquire_lease("o")
    writer = StudyWriter(store)
    writer.start()
    return clock, store, writer


async def _journal_attempt(writer: StudyWriter, *, sid: str, attempt_id: str) -> None:
    await writer.append(
        EventEnvelope(
            study_id=sid,
            type="AttemptStarted",
            entity_key=attempt_id,
            idem_key=make_idem("AttemptStarted", attempt_id),
            payload={
                "attempt_id": attempt_id,
                "n": 1,
                "dispatch_id": f"dispatch:{attempt_id}",
                "route_returned": False,
                "closed": False,
                "task_id": f"{sid}/LIT-0",
                "study_id": sid,
                "provenance": "real",
                "deadline": datetime.now(UTC).isoformat(),
                "inputs": [],
                "tainted_context": False,
                "limitations": [],
                "attempt_role": "agent",
            },
            ts=datetime.now(UTC),
            schema_version=writer_schema_version("AttemptStarted"),
        )
    )


@pytest.fixture
def registry_fetch():
    _reset_registry_for_tests()
    _register_tool_for_tests(
        RegisteredToolSpec(name="papers_get", effect="read", is_fetch=True)
    )
    _register_tool_for_tests(
        RegisteredToolSpec(name="catalog_read", effect="read", is_fetch=False)
    )
    freeze_registry()
    yield
    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_tool_missing_refuses_before_invoke(tmp_path: Path) -> None:
    clock, store, writer = await _writer_store(tmp_path)
    tool = _CountingTool(ToolSpec(name="absent", effect="read"))
    ctx = CallCtx(
        study_id="st_inv",
        task_key="LIT-0",
        attempt_id="a#1",
        hitl_policy=HitlPolicy(),
    )
    try:
        with pytest.raises(CatalogInvokeRefuse, match="tool_missing"):
            await invoke_after_catalog_resolve(
                ToolNeed(study_id="st_inv", task_id="st_inv/E", tool_name="absent"),
                catalog=McpToolCatalog(initial_version=0, tools=()),
                expected_catalog_version=0,
                tool=tool,
                args={},
                ctx=ctx,
                writer=writer,
                clock=clock,
            )
        assert tool.calls == []
        ev = (await store.read_all())[0]
        assert ev.type == "ToolResolved"
        assert ev.payload["status"] == "tool_missing"
        assert ev.payload["match"]["kind"] == "none"
    finally:
        await writer.stop()


@pytest.mark.asyncio
async def test_name_mismatch_refuses_before_invoke(tmp_path: Path) -> None:
    clock, _store, writer = await _writer_store(tmp_path)
    tool = _CountingTool(ToolSpec(name="other", effect="read"))
    ctx = CallCtx(
        study_id="st_inv",
        task_key="LIT-0",
        attempt_id="a#1",
        hitl_policy=HitlPolicy(),
    )
    try:
        with pytest.raises(CatalogInvokeRefuse, match=r"tool\.spec\.name"):
            await invoke_after_catalog_resolve(
                ToolNeed(study_id="st_inv", task_id="st_inv/E", tool_name="papers_get"),
                catalog=McpToolCatalog(
                    initial_version=1, tools=(_catalog_tool("papers_get"),)
                ),
                expected_catalog_version=1,
                tool=tool,
                args={},
                ctx=ctx,
                writer=writer,
                clock=clock,
            )
        assert tool.calls == []
    finally:
        await writer.stop()


@pytest.mark.asyncio
async def test_mcp_presence_without_is_fetch_no_source_fetched(
    tmp_path: Path, registry_fetch: None
) -> None:
    """Catalog + provider=mcp ToolSpec alone ≠ SourceFetched (registry is_fetch gates)."""
    clock, store, writer = await _writer_store(tmp_path)
    await _journal_attempt(writer, sid="st_inv", attempt_id="a#1")
    arts = LocalArtifactStore(tmp_path / "arts")
    # Advisory is_fetch=True on ToolSpec must not mint SourceFetched.
    tool = _CountingTool(
        ToolSpec(name="catalog_read", provider="mcp", effect="read", is_fetch=True)
    )
    ctx = CallCtx(
        study_id="st_inv",
        task_key="LIT-0",
        attempt_id="a#1",
        hitl_policy=HitlPolicy(),
    )
    try:
        assert resolve_is_fetch("catalog_read") is False
        resolution, out = await invoke_after_catalog_resolve(
            ToolNeed(study_id="st_inv", task_id="st_inv/E", tool_name="catalog_read"),
            catalog=McpToolCatalog(
                initial_version=2, tools=(_catalog_tool("catalog_read"),)
            ),
            expected_catalog_version=2,
            tool=tool,
            args={"q": 1},
            ctx=ctx,
            writer=writer,
            clock=clock,
            artifacts=arts,
        )
        assert resolution.status == "tool_found"
        assert out.data == {"ok": True}
        assert len(tool.calls) == 1
        types = [e.type for e in await store.read_all()]
        assert "ToolResolved" in types
        assert "SourceFetched" not in types
        assert "AttemptTainted" not in types
    finally:
        await writer.stop()


@pytest.mark.asyncio
async def test_tool_found_is_fetch_source_fetched(
    tmp_path: Path, registry_fetch: None
) -> None:
    clock, store, writer = await _writer_store(tmp_path)
    await _journal_attempt(writer, sid="st_inv", attempt_id="a#1")
    arts = LocalArtifactStore(tmp_path / "arts")
    tool = _FetchBodyTool(
        ToolSpec(name="papers_get", effect="read", is_fetch=False),
        arts,
        b"body",
    )
    ctx = CallCtx(
        study_id="st_inv",
        task_key="LIT-0",
        attempt_id="a#1",
        hitl_policy=HitlPolicy(),
    )
    try:
        assert resolve_is_fetch("papers_get") is True
        resolution, out = await invoke_after_catalog_resolve(
            ToolNeed(study_id="st_inv", task_id="st_inv/E", tool_name="papers_get"),
            catalog=McpToolCatalog(
                initial_version=3, tools=(_catalog_tool("papers_get"),)
            ),
            expected_catalog_version=3,
            tool=tool,
            args={"url": "https://agent.example/ignored"},
            ctx=ctx,
            writer=writer,
            clock=clock,
            artifacts=arts,
        )
        assert resolution.status == "tool_found"
        assert resolution.match.kind == "exact"
        assert out.artifacts
        events = await store.read_all()
        assert any(e.type == "SourceFetched" for e in events)
        sf = next(e for e in events if e.type == "SourceFetched")
        assert sf.payload["url"] == "https://normalized.example/p"
        # AttemptStarted provenance remains fail-closed parseable.
        started = next(e for e in events if e.type == "AttemptStarted")
        assert parse_provenance(started.payload.get("provenance"), owner="test") == "real"
    finally:
        await writer.stop()


def test_mcp_catalog_tool_forbids_is_fetch_effect() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        McpCatalogTool.model_validate(
            {
                "name": "x",
                "description": None,
                "input_schema": {},
                "provider": "mcp",
                "is_fetch": True,
            }
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        McpCatalogTool.model_validate(
            {
                "name": "x",
                "description": None,
                "input_schema": {},
                "provider": "mcp",
                "effect": "read",
            }
        )


def test_parse_provenance_still_fail_closed() -> None:
    with pytest.raises(ValueError, match="provenance required"):
        parse_provenance(None, owner="wiring")
    with pytest.raises(ValueError, match="unknown provenance"):
        parse_provenance("invented", owner="wiring")
