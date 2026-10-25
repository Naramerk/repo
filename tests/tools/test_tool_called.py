"""TG-Call (#88): ToolCalled journaled on invoke_tool after ToolResolved."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.events import EventEnvelope, EventType, make_idem
from coscientist.core.events.payload_schemas import (
    PAYLOAD_MODELS,
    ToolCalledPayload,
    validate_event_payload,
)
from coscientist.core.events.schema_gate import _payload_validators
from coscientist.core.events.schema_versions import TOOL_CALLED_SCHEMA_VERSION
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import HitlPolicy, ToolCallRef
from coscientist.core.execution.tool_registry import (
    RegisteredToolSpec,
    _register_tool_for_tests,
    _reset_registry_for_tests,
    freeze_registry,
)
from coscientist.tools.contract import CallCtx, ToolError, ToolOutput, ToolSpec
from coscientist.tools.effects import invoke_tool
from coscientist.tools.mcp_catalog import McpCatalogTool, McpToolCatalog
from coscientist.tools.resolve import ToolNeed, invoke_after_catalog_resolve


@dataclass
class _OkTool:
    spec: ToolSpec
    calls: list[dict[str, object]] = field(default_factory=list)

    async def call(self, args: dict[str, object], ctx: CallCtx) -> ToolOutput:
        self.calls.append(dict(args))
        return ToolOutput(data={"ok": True})


@dataclass
class _FailTool:
    spec: ToolSpec

    async def call(self, args: dict[str, object], ctx: CallCtx) -> ToolOutput:
        raise ToolError("local", "boom")


def _ref() -> ToolCallRef:
    ts = datetime(2026, 10, 25, tzinfo=UTC)
    return ToolCallRef(
        call_id="a#1:1", tool="t", args_digest="d" * 64, started_at=ts, ended_at=ts
    )


async def _runtime(tmp_path: Path) -> tuple[FakeClock, SqliteEventStore, StudyWriter]:
    clock = FakeClock(datetime(2026, 10, 25, tzinfo=UTC))
    store = SqliteEventStore("st_tc", tmp_path / "j.db", clock=clock)
    await store.acquire_lease("o")
    writer = StudyWriter(store)
    writer.start()
    return clock, store, writer


async def _attempt(writer: StudyWriter) -> None:
    await writer.append(
        EventEnvelope(
            study_id="st_tc",
            type="AttemptStarted",
            entity_key="a#1",
            idem_key=make_idem("AttemptStarted", "a#1"),
            payload={
                "attempt_id": "a#1",
                "n": 1,
                "dispatch_id": "dispatch:a#1",
                "route_returned": False,
                "closed": False,
                "task_id": "st_tc/LIT-0",
                "study_id": "st_tc",
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
def registry_read():
    _reset_registry_for_tests()
    _register_tool_for_tests(RegisteredToolSpec(name="catalog_read", effect="read"))
    freeze_registry()
    yield
    _reset_registry_for_tests()


def test_payload_gate_and_outcome_refuse() -> None:
    assert set(PAYLOAD_MODELS) == set(get_args(EventType)) == set(_payload_validators())
    assert PAYLOAD_MODELS["ToolCalled"] is ToolCalledPayload
    raw: dict[str, object] = {
        "ref": _ref().model_dump(mode="json"),
        "outcome": "ok",
        "error_kind": None,
        "duration_ms": 0,
    }
    assert validate_event_payload("ToolCalled", raw) is raw
    with pytest.raises(ValidationError, match="error_kind"):
        ToolCalledPayload(ref=_ref(), outcome="ok", error_kind="local", duration_ms=0)
    with pytest.raises(ValidationError, match="error_kind"):
        ToolCalledPayload(ref=_ref(), outcome="error", error_kind=None, duration_ms=1)


@pytest.mark.asyncio
async def test_resolve_then_invoke_journals_ok(
    tmp_path: Path, registry_read: None
) -> None:
    clock, store, writer = await _runtime(tmp_path)
    await _attempt(writer)
    tool = _OkTool(ToolSpec(name="catalog_read", effect="read"))
    ctx = CallCtx(
        study_id="st_tc", task_key="LIT-0", attempt_id="a#1", hitl_policy=HitlPolicy()
    )
    try:
        resolution, out = await invoke_after_catalog_resolve(
            ToolNeed(study_id="st_tc", task_id="st_tc/E", tool_name="catalog_read"),
            catalog=McpToolCatalog(
                initial_version=1,
                tools=(
                    McpCatalogTool(
                        name="catalog_read",
                        description="d",
                        input_schema={"type": "object"},
                        provider="mcp",
                    ),
                ),
            ),
            expected_catalog_version=1,
            tool=tool,
            args={"q": 1},
            ctx=ctx,
            writer=writer,
            clock=clock,
        )
        assert resolution.status == "tool_found" and out.data == {"ok": True}
        types = [e.type for e in await store.read_all()]
        assert types.index("ToolResolved") < types.index("ToolCalled")
        called = next(e for e in await store.read_all() if e.type == "ToolCalled")
        assert called.schema_version == TOOL_CALLED_SCHEMA_VERSION
        p = validate_event_payload("ToolCalled", called.payload)
        assert p["outcome"] == "ok" and p["error_kind"] is None
        assert p["ref"]["tool"] == "catalog_read" and p["ref"]["call_id"] == "a#1:1"
        assert type(p["duration_ms"]) is int and p["duration_ms"] >= 0
    finally:
        await writer.stop()


@pytest.mark.asyncio
async def test_invoke_error_and_deny_paths(
    tmp_path: Path, registry_read: None
) -> None:
    _clock, store, writer = await _runtime(tmp_path)
    del _clock
    await _attempt(writer)
    ctx = CallCtx(
        study_id="st_tc", task_key="LIT-0", attempt_id="a#1", hitl_policy=HitlPolicy()
    )
    try:
        with pytest.raises(ToolError, match="boom"):
            await invoke_tool(
                _FailTool(ToolSpec(name="catalog_read", effect="read")),
                {},
                ctx,
                writer=writer,
            )
        err = next(e for e in await store.read_all() if e.type == "ToolCalled")
        p = validate_event_payload("ToolCalled", err.payload)
        assert p["outcome"] == "error" and p["error_kind"] == "local"

        _reset_registry_for_tests()
        _register_tool_for_tests(RegisteredToolSpec(name="mutate", effect="write"))
        freeze_registry()
        arts = LocalArtifactStore(tmp_path / "arts")
        wctx = CallCtx(
            study_id="st_tc",
            task_key="EXP-0",
            attempt_id="a#1",
            hitl_policy=HitlPolicy(),
        )
        before = {e.idem_key for e in await store.read_all() if e.type == "ToolCalled"}
        with pytest.raises(ToolError, match="side_effect"):
            await invoke_tool(
                _OkTool(ToolSpec(name="mutate", effect="read")),
                {"x": 1},
                wctx,
                writer=writer,
                artifacts=arts,
            )
        types = [e.type for e in await store.read_all()]
        assert "SideEffectDenied" in types
        after = {e.idem_key for e in await store.read_all() if e.type == "ToolCalled"}
        assert after == before
    finally:
        await writer.stop()
        _reset_registry_for_tests()
