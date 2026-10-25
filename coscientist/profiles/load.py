"""Packaged YAML/JSON → ``Profile.model_validate_json`` (no list→tuple shim).

Hard dep (runtime tool invoke): G3 catalog resolve #65 + #157 — not imported here
(import-linter: profiles ↛ tools). Load only checks exact name ⊆ tool catalog.
"""

from __future__ import annotations

import json
from importlib import resources

import yaml

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
from coscientist.profiles.models import Profile

_DATA = resources.files("coscientist.profiles").joinpath("packaged")
_TOOL_CATALOG_RESOURCE = "tool_catalog.json"
_PRODUCTION_CATALOG_RESOURCE = "production_catalog.json"


def _read_data_text(name: str) -> str:
    path = _DATA.joinpath(name)
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        raise ProfileLoadError(f"packaged profile data missing or unreadable: {name!r}") from exc


def load_tool_catalog() -> ProfileToolCatalog:
    """Load the exact versioned profile tool allowlist from package data."""
    return ProfileToolCatalog.model_validate_json(_read_data_text(_TOOL_CATALOG_RESOURCE))


def load_production_catalog() -> ProductionProfileCatalog:
    """Load the versioned production profile index from package data."""
    return ProductionProfileCatalog.model_validate_json(
        _read_data_text(_PRODUCTION_CATALOG_RESOURCE)
    )


def profile_from_yaml_text(text: str) -> Profile:
    """YAML mapping → JSON → ``Profile.model_validate_json`` (lists OK only via JSON)."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileLoadError("profile YAML failed to parse") from exc
    if not isinstance(raw, dict):
        raise ProfileLoadError("profile root must be a mapping")
    try:
        payload = json.dumps(raw)
    except (TypeError, ValueError) as exc:
        raise ProfileLoadError("profile YAML is not JSON-serializable") from exc
    return Profile.model_validate_json(payload)


def _assert_tools_exact(profile: Profile, catalog: ProfileToolCatalog) -> None:
    missing: list[str] = []
    for role_name, role in profile.roles.items():
        for tool in role.tools:
            if not catalog.contains(tool):
                missing.append(f"{role_name}:{tool}")
    if missing:
        raise UnknownToolError(
            "role tools not in exact profile tool catalog: " + ", ".join(sorted(missing))
        )


def _assert_entry_matches(profile: Profile, entry: ProfileCatalogEntry) -> None:
    if profile.name != entry.name:
        raise ProfileLoadError(
            f"profile name mismatch: file={profile.name!r} catalog={entry.name!r}"
        )
    if profile.profile_version != entry.profile_version:
        raise ProfileLoadError(
            "profile_version mismatch: "
            f"file={profile.profile_version} catalog={entry.profile_version}"
        )


def load_profile(
    name: str,
    *,
    expected_tool_catalog_version: int | None = None,
) -> Profile:
    """Load one production profile by exact catalog name; unknown → refuse.

    ``expected_tool_catalog_version`` when set must equal the packaged tool
    catalog version (same pin idea as #65 ``expected_catalog_version``).
    """
    index = load_production_catalog()
    entry = index.get(name)
    if entry is None:
        raise UnknownProfileError(f"unknown profile: {name!r}")

    tool_catalog = load_tool_catalog()
    if tool_catalog.catalog_version != entry.tool_catalog_version:
        raise CatalogVersionError(
            "tool catalog_version mismatch: "
            f"catalog={tool_catalog.catalog_version} "
            f"profile_entry={entry.tool_catalog_version}"
        )
    if expected_tool_catalog_version is not None:
        if type(expected_tool_catalog_version) is not int or isinstance(
            expected_tool_catalog_version, bool
        ):
            raise CatalogVersionError(
                "expected_tool_catalog_version must be exact int, "
                f"got {expected_tool_catalog_version!r}"
            )
        if tool_catalog.catalog_version != expected_tool_catalog_version:
            raise CatalogVersionError(
                "tool catalog_version mismatch: "
                f"catalog={tool_catalog.catalog_version} "
                f"expected={expected_tool_catalog_version}"
            )

    profile = profile_from_yaml_text(_read_data_text(entry.resource))
    _assert_entry_matches(profile, entry)
    _assert_tools_exact(profile, tool_catalog)
    return profile


def list_production_profile_names() -> tuple[str, ...]:
    """Exact names present in the production catalog (sorted)."""
    return tuple(sorted(e.name for e in load_production_catalog().profiles))
