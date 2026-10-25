"""Exact catalog resolve → ToolResolved; optional invoke via registry (DESIGN §6.3).

Catalog answers name presence only. ``effect`` / ``is_fetch`` stay on
``RegisteredToolSpec`` and are applied only inside ``invoke_tool``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from coscientist.core.events.clock import Clock
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.payload_schemas import MatchInfo, ToolResolvedPayload
from coscientist.core.events.ports import EventStore
from coscientist.core.events.schema_versions import TOOL_RESOLVED_SCHEMA_VERSION
from coscientist.tools.mcp_catalog import McpCatalogError, McpToolCatalog

if TYPE_CHECKING:
    from coscientist.core.events.writer import StudyWriter
    from coscientist.core.execution.artifacts import LocalArtifactStore
    from coscientist.tools.contract import CallCtx, Tool, ToolOutput
    from coscientist.tools.effects import EffectIndex


class ToolNeed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    study_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)


class CatalogInvokeRefuse(ValueError):
    """Refuse ``invoke_tool`` after catalog resolve (missing / name mismatch)."""


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
            idem_key=make_idem("ToolResolved", resolution.task_id),
            payload=resolution.model_dump(mode="json"),
            ts=clock.now(),
            schema_version=TOOL_RESOLVED_SCHEMA_VERSION,
        )
    )
    return resolution


async def invoke_after_catalog_resolve(
    need: ToolNeed,
    *,
    catalog: McpToolCatalog,
    expected_catalog_version: int,
    tool: Tool,
    args: dict[str, object],
    ctx: CallCtx,
    writer: StudyWriter,
    clock: Clock,
    artifacts: LocalArtifactStore | None = None,
    effect_index: EffectIndex | None = None,
) -> tuple[ToolResolvedPayload, ToolOutput]:
    """Exact catalog resolve, then ``invoke_tool`` (registry owns effect / is_fetch).

    Journals ``ToolResolved`` first. Invokes only when ``status == "tool_found"``
    and ``tool.spec.name`` equals the resolved catalog name. Does not read
    ``effect`` / ``is_fetch`` from ``McpCatalogTool`` (those fields are forbidden).
    """
    from coscientist.tools.effects import invoke_tool

    resolution = await resolve_against_catalog(
        need,
        catalog=catalog,
        store=writer.store,
        clock=clock,
        expected_catalog_version=expected_catalog_version,
    )
    if resolution.status != "tool_found":
        raise CatalogInvokeRefuse(
            f"refuse invoke: status={resolution.status!r} (require tool_found)"
        )
    resolved = resolution.tool
    if resolved is None:
        raise CatalogInvokeRefuse("refuse invoke: tool_found without tool name")
    if tool.spec.name != resolved:
        raise CatalogInvokeRefuse(
            f"refuse invoke: tool.spec.name={tool.spec.name!r} != resolved={resolved!r}"
        )
    # Registry effect / is_fetch applied only inside invoke_tool — not catalog.
    output = await invoke_tool(
        tool,
        dict(args),
        ctx,
        writer=writer,
        artifacts=artifacts,
        effect_index=effect_index,
    )
    return resolution, output
