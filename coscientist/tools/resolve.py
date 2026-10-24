"""Exact catalog resolve → one ToolResolved (DESIGN §6.3 / CAPABILITIES #8)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from coscientist.core.events.clock import Clock
from coscientist.core.events.events import TOOL_RESOLVED_TYPE, UnstampedEvent, make_idem
from coscientist.core.events.ports import EventStore
from coscientist.core.events.schema_versions import TOOL_RESOLVED_SCHEMA_VERSION
from coscientist.core.events.tool_resolved import MatchInfo, ToolResolvedPayload
from coscientist.tools.mcp_catalog import McpCatalogError, McpToolCatalog


class ToolNeed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    study_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)


async def resolve_against_catalog(
    need: ToolNeed,
    *,
    catalog: McpToolCatalog,
    store: EventStore,
    clock: Clock,
    expected_catalog_version: int,
) -> ToolResolvedPayload:
    if type(expected_catalog_version) is not int or isinstance(expected_catalog_version, bool):
        raise McpCatalogError(
            f"expected_catalog_version must be exact int, got {expected_catalog_version!r}"
        )
    version = catalog.version
    if version != expected_catalog_version:
        raise McpCatalogError(
            f"catalog_version mismatch: catalog={version} expected={expected_catalog_version}"
        )
    found = catalog.get(need.tool_name)
    if found is not None:
        resolution = ToolResolvedPayload(
            task_id=need.task_id,
            status="tool_found",
            tool=found.name,
            catalog_version=version,
            match=MatchInfo(kind="exact", ranked=[found.name]),
        )
    else:
        resolution = ToolResolvedPayload(
            task_id=need.task_id,
            status="tool_missing",
            tool=None,
            catalog_version=version,
            match=MatchInfo(kind="none", ranked=[]),
        )
    await store.append(
        UnstampedEvent(
            study_id=need.study_id,
            type="ToolResolved",
            entity_key=resolution.task_id,
            idem_key=make_idem(TOOL_RESOLVED_TYPE, resolution.task_id),
            payload=resolution.model_dump(mode="json"),
            ts=clock.now(),
            schema_version=TOOL_RESOLVED_SCHEMA_VERSION,
        )
    )
    return resolution
