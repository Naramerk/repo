"""Versioned research profiles (DESIGN §8). Domain knowledge stays in config."""

from coscientist.profiles.catalog import (
    ProductionProfileCatalog,
    ProfileCatalogEntry,
    ProfileToolCatalog,
)
from coscientist.profiles.errors import (
    CatalogVersionError,
    ProfileLoadError,
    UnknownProfileError,
    UnknownToolError,
)
from coscientist.profiles.load import (
    list_production_profile_names,
    load_production_catalog,
    load_profile,
    load_tool_catalog,
    profile_from_yaml_text,
)
from coscientist.profiles.models import AlembicMode, Profile, RoleConfig, RouteConfig

__all__ = [
    "AlembicMode",
    "CatalogVersionError",
    "ProductionProfileCatalog",
    "Profile",
    "ProfileCatalogEntry",
    "ProfileLoadError",
    "ProfileToolCatalog",
    "RoleConfig",
    "RouteConfig",
    "UnknownProfileError",
    "UnknownToolError",
    "list_production_profile_names",
    "load_production_catalog",
    "load_profile",
    "load_tool_catalog",
    "profile_from_yaml_text",
]
