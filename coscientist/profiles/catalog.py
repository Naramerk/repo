"""Exact, versioned production profile index + profile tool allowlist.

Aligns conceptually with G3 (#65 / #157): exact name match only, version pin,
no blanket allow of unknown tools. Runtime ``ToolResolved`` / ``invoke_tool``
still hard-depends on merge of #65+#157 — this package only refuses at load.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from coscientist.mcp.config import Identifier


class ProfileToolCatalog(BaseModel):
    """Exact tool names a packaged profile may reference (DESIGN §8 / §6.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    catalog_version: StrictInt = Field(ge=0)
    tools: tuple[Identifier, ...]

    @model_validator(mode="after")
    def _unique_tools(self) -> ProfileToolCatalog:
        if len(self.tools) != len(set(self.tools)):
            raise ValueError("ProfileToolCatalog.tools names must be unique")
        return self

    def contains(self, name: str) -> bool:
        return name in self.tools


class ProfileCatalogEntry(BaseModel):
    """One production profile packaged under ``profiles/packaged/``."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: Identifier
    profile_version: StrictInt = Field(ge=1)
    resource: Identifier
    tool_catalog_version: StrictInt = Field(ge=0)


class ProductionProfileCatalog(BaseModel):
    """Versioned index of known production profiles (unknown name → refuse)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    catalog_version: StrictInt = Field(ge=1)
    profiles: tuple[ProfileCatalogEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> ProductionProfileCatalog:
        names = [e.name for e in self.profiles]
        if len(names) != len(set(names)):
            raise ValueError("ProductionProfileCatalog profile names must be unique")
        return self

    def get(self, name: str) -> ProfileCatalogEntry | None:
        for entry in self.profiles:
            if entry.name == name:
                return entry
        return None
