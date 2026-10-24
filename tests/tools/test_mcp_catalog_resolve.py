"""G3: McpToolCatalog + exact ToolResolved."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, get_args

import pytest
from pydantic import ValidationError

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.errors import Idempotent, JournalPayloadError
from coscientist.core.events.events import TOOL_RESOLVED_TYPE, EventType, UnstampedEvent
from coscientist.core.events.payload_schemas import PAYLOAD_MODELS
from coscientist.core.events.schema_gate import _payload_validators
from coscientist.core.events.schema_versions import TOOL_RESOLVED_SCHEMA_VERSION
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.events.tool_resolved import MatchInfo, ToolResolvedPayload
from coscientist.core.events.tool_resolved import validate_tool_resolved_payload as _vtr
from coscientist.tools.contract import ToolProvider
from coscientist.tools.mcp_catalog import McpCatalogError, McpCatalogTool, McpToolCatalog
from coscientist.tools.resolve import ToolNeed, resolve_against_catalog

_X = {"name": "x", "description": None, "input_schema": {}}
_TE = {"study_id": "s", "task_id": "s/E", "tool_name": "t", "extra": 1}

def _tool(name: str, *, provider: ToolProvider) -> McpCatalogTool:
    return McpCatalogTool(
        name=name, description="d", input_schema={"type": "object"}, provider=provider,
    )

async def _store(tmp_path: Path) -> tuple[FakeClock, SqliteEventStore, Path]:
    path = tmp_path / "j.db"
    clock = FakeClock(datetime(2026, 10, 24, tzinfo=UTC))
    store = SqliteEventStore("st1", path, clock=clock)
    await store.acquire_lease("o")
    return clock, store, path

def test_catalog_seed_get_register() -> None:
    cat = McpToolCatalog(initial_version=2, tools=(_tool("a", provider="mcp"),))
    assert cat.version == 2
    assert cat.get("a") is not None
    assert cat.get("missing") is None
    assert cat.register(_tool("b", provider="alembic")) == 3

def test_catalog_register_duplicate_refused() -> None:
    cat = McpToolCatalog(initial_version=0, tools=(_tool("a", provider="mcp"),))
    with pytest.raises(McpCatalogError, match="already"):
        cat.register(_tool("a", provider="mcp"))

def test_catalog_refuse_duplicate_seed() -> None:
    tools = (_tool("a", provider="mcp"), _tool("a", provider="mcp"))
    with pytest.raises(McpCatalogError, match="duplicate seed"):
        McpToolCatalog(initial_version=0, tools=tools)

@pytest.mark.parametrize(
    ("initial_version", "match"),
    [(True, "exact int"), (-1, ">= 0")],
    ids=["not-int", "negative"],
)
def test_catalog_refuse_initial_version(initial_version: object, match: str) -> None:
    with pytest.raises(McpCatalogError, match=match):
        McpToolCatalog(**{"initial_version": initial_version, "tools": ()})

@pytest.mark.parametrize(
    ("factory", "err", "loc"),
    [
        (lambda: McpCatalogTool.model_validate({**_X, "provider": "mcp", "effect": "r"}),
         "extra_forbidden", ("effect",)),
        (lambda: McpCatalogTool.model_validate({**_X, "provider": "fedot"}),
         "literal_error", ("provider",)),
        (lambda: ToolNeed.model_validate(_TE), "extra_forbidden", ("extra",)),
        (lambda: MatchInfo.model_validate({"kind": "semantic", "ranked": ["t"]}),
         "literal_error", ("kind",)),
    ],
    ids=["tool-extra", "bad-provider", "need-extra", "semantic-kind"],
)
def test_strict_extra_refuse(factory: Callable[[], Any], err: str, loc: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError, match=err) as ei:
        factory()
    err0 = ei.value.errors()[0]
    assert err0["type"] == err
    assert err0["loc"] == loc

@pytest.mark.parametrize(
    ("kw", "msg"),
    [
        ({"status": "tool_found", "tool": None, "match": MatchInfo(kind="exact", ranked=[])}, "requires tool"),
        ({"status": "tool_found", "tool": "x", "match": MatchInfo(kind="none", ranked=[])}, r"match\.kind=exact"),
        ({"status": "tool_found", "tool": "x", "match": MatchInfo(kind="exact", ranked=["y"])}, r"ranked=\[tool\]"),
        ({"status": "tool_missing", "tool": "x", "match": MatchInfo(kind="none", ranked=[])}, "tool=None"),
    ],
    ids=["found-no-tool", "found-kind", "ranked", "miss-tool"],
)
def test_resolution_validator(kw: dict[str, Any], msg: str) -> None:
    with pytest.raises(ValidationError, match=msg):
        ToolResolvedPayload(task_id="t", catalog_version=0, **kw)

@pytest.mark.asyncio
async def test_version_mismatch_refused(tmp_path: Path) -> None:
    clock, store, _ = await _store(tmp_path)
    with pytest.raises(McpCatalogError, match="catalog_version mismatch"):
        await resolve_against_catalog(
            ToolNeed(study_id="st1", task_id="st1/E", tool_name="x"),
            catalog=McpToolCatalog(initial_version=3, tools=()),
            store=store, clock=clock, expected_catalog_version=2,
        )
    assert await store.read_all() == []

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [True, 1.5], ids=["bool", "float"])
async def test_expected_catalog_version_not_int(tmp_path: Path, bad: object) -> None:
    clock, store, _ = await _store(tmp_path)
    with pytest.raises(McpCatalogError, match="exact int"):
        await resolve_against_catalog(
            ToolNeed(study_id="st1", task_id="st1/E", tool_name="x"),
            catalog=McpToolCatalog(initial_version=0, tools=()),
            store=store, clock=clock, **{"expected_catalog_version": bad},
        )

@pytest.mark.asyncio
async def test_exact_found_journals(tmp_path: Path) -> None:
    clock, store, _ = await _store(tmp_path)
    out = await resolve_against_catalog(
        ToolNeed(study_id="st1", task_id="st1/E1", tool_name="t"),
        catalog=McpToolCatalog(initial_version=1, tools=(_tool("t", provider="mcp"),)),
        store=store, clock=clock, expected_catalog_version=1,
    )
    assert out.status == "tool_found"
    assert out.tool == "t"
    ev = (await store.read_all())[0]
    assert ev.schema_version == TOOL_RESOLVED_SCHEMA_VERSION
    assert _vtr(ev.payload)["tool"] == "t"

@pytest.mark.asyncio
async def test_miss_journals(tmp_path: Path) -> None:
    clock, store, _ = await _store(tmp_path)
    miss = await resolve_against_catalog(
        ToolNeed(study_id="st1", task_id="st1/E2", tool_name="absent"),
        catalog=McpToolCatalog(initial_version=0, tools=()),
        store=store, clock=clock, expected_catalog_version=0,
    )
    assert miss.status == "tool_missing"
    assert miss.match.kind == "none"

@pytest.mark.asyncio
async def test_miss_idempotent_replay(tmp_path: Path) -> None:
    clock, store, _ = await _store(tmp_path)
    cat = McpToolCatalog(initial_version=0, tools=())
    need = ToolNeed(study_id="st1", task_id="st1/E2", tool_name="absent")
    kw = dict(catalog=cat, store=store, clock=clock, expected_catalog_version=0)
    await resolve_against_catalog(need, **kw)
    with pytest.raises(Idempotent, match="idempotent replay"):
        await resolve_against_catalog(need, **kw)

async def _append_tr(
    store: SqliteEventStore, clock: FakeClock, entity: str, raw: dict[str, Any],
) -> None:
    await store.append(UnstampedEvent(
        study_id="st1", type=TOOL_RESOLVED_TYPE, entity_key=entity,
        idem_key=f"{TOOL_RESOLVED_TYPE}:{entity}", payload=raw,
        ts=clock.now(), schema_version=TOOL_RESOLVED_SCHEMA_VERSION,
    ))

@pytest.mark.asyncio
async def test_semantic_kind_refused_on_replay(tmp_path: Path) -> None:
    clock, store, _ = await _store(tmp_path)
    await _append_tr(store, clock, "st1/SEM", {
        "task_id": "st1/SEM", "status": "tool_found", "tool": "x",
        "catalog_version": 0, "match": {"kind": "semantic", "ranked": ["x"]},
    })
    with pytest.raises(JournalPayloadError, match="payload schema refuse") as ei:
        await store.read_all()
    assert ei.value.loc == ("match", "kind")
    assert ei.value.error_type == "literal_error"

@pytest.mark.asyncio
async def test_broken_json_payload_refused(tmp_path: Path) -> None:
    clock, store, path = await _store(tmp_path)
    await _append_tr(store, clock, "st1/BJ", {
        "task_id": "st1/BJ", "status": "tool_missing", "tool": None,
        "catalog_version": 0, "match": {"kind": "none", "ranked": []},
    })
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE events SET payload=? WHERE entity_key=?", ("{bad", "st1/BJ"))
        conn.commit()
    with pytest.raises(JournalPayloadError, match="broken JSON") as ei:
        await store.read_all()
    assert ei.value.loc == ("payload",)
    assert ei.value.error_type == "json_invalid"

def test_payload_validators_include_tool_resolved() -> None:
    reg = _payload_validators()
    assert set(reg) == set(get_args(EventType))
    assert reg[TOOL_RESOLVED_TYPE] is _vtr
    assert TOOL_RESOLVED_TYPE not in PAYLOAD_MODELS
    assert set(PAYLOAD_MODELS) == set(get_args(EventType)) - {TOOL_RESOLVED_TYPE}
