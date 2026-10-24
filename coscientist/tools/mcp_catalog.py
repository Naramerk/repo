"""Versioned MCP tool catalog (§6.3). Not ``tool_registry``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from coscientist.tools.contract import ToolProvider


class McpCatalogTool(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str = Field(min_length=1)
    description: str | None
    input_schema: dict[str, object]
    provider: ToolProvider


class McpCatalogError(ValueError):
    """Typed refuse: duplicate, bad version, version mismatch."""


class McpToolCatalog:
    def __init__(self, *, initial_version: int, tools: tuple[McpCatalogTool, ...]) -> None:
        if type(initial_version) is not int or isinstance(initial_version, bool):
            raise McpCatalogError(
                f"initial_version must be exact int, got {initial_version!r}"
            )
        if initial_version < 0:
            raise McpCatalogError("initial_version must be >= 0")
        self._version = initial_version
        self._tools: dict[str, McpCatalogTool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise McpCatalogError(f"duplicate seed tool name: {tool.name!r}")
            self._tools[tool.name] = tool

    @property
    def version(self) -> int:
        return self._version

    def get(self, name: str) -> McpCatalogTool | None:
        return self._tools.get(name)

    def register(self, tool: McpCatalogTool) -> int:
        if tool.name in self._tools:
            raise McpCatalogError(
                f"tool already in catalog: {tool.name!r} (replacement forbidden)"
            )
        self._tools[tool.name] = tool
        self._version += 1
        return self._version
