"""Versioned research profiles (DESIGN §8). Domain knowledge stays in config."""

from coscientist.profiles.models import (
    AlembicMode,
    Profile,
    RoleConfig,
    RoleName,
    RouteConfig,
)

Identifier = RoleName  # same Annotated alias; avoids profiles→mcp in __init__

__all__ = [
    "AlembicMode",
    "Identifier",
    "Profile",
    "RoleConfig",
    "RoleName",
    "RouteConfig",
]
