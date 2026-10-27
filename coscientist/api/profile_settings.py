"""Revisioned local preferences. Trusted runtime catalog owns available choices."""
from __future__ import annotations

from collections.abc import Callable
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coscientist.api.local_profiles import LocalProfiles, ProfileRefuse
from coscientist.core.events.digest import canonical_json, sha256_hex


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SettingsValues(SettingsModel):
    role_choices: dict[str, str]
    enabled_server_ids: list[str]
    enabled_tool_ids: list[str]
    coder_enabled: bool


class ModelChoice(SettingsModel):
    id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1)
    model: str = Field(min_length=1)


class RoleChoices(SettingsModel):
    id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1)
    choices: list[ModelChoice]


class ToolChoice(SettingsModel):
    id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1)


class ServerChoices(SettingsModel):
    id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1)
    tools: list[ToolChoice]


class CoderChoices(SettingsModel):
    available: bool
    tool_ids: list[str]


class SettingsCatalog(SettingsModel):
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    roles: list[RoleChoices]
    mcp_servers: list[ServerChoices]
    coder: CoderChoices
    preset_ids: list[str]
    defaults: SettingsValues

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        groups = [[role.id for role in self.roles], [server.id for server in self.mcp_servers],
                  [tool.id for server in self.mcp_servers for tool in server.tools] + self.coder.tool_ids,
                  self.preset_ids]
        groups.extend([choice.id for choice in role.choices] for role in self.roles)
        if any(len(group) != len(set(group)) for group in groups):
            raise ValueError("settings catalog IDs must be unique within their namespace")
        validate_settings(self.defaults, self)
        return self


CatalogProvider = Callable[[], SettingsCatalog]


def validate_settings(values: SettingsValues, catalog: SettingsCatalog) -> None:
    allowed_roles = {role.id: {choice.id for choice in role.choices} for role in catalog.roles}
    if set(values.role_choices) != set(allowed_roles):
        raise ProfileRefuse("settings_roles_incomplete_or_unknown", 400)
    if any(choice not in allowed_roles[role] for role, choice in values.role_choices.items()):
        raise ProfileRefuse("settings_model_choice_unknown", 400)
    servers = set(values.enabled_server_ids)
    tools = set(values.enabled_tool_ids)
    if len(servers) != len(values.enabled_server_ids) or len(tools) != len(values.enabled_tool_ids):
        raise ProfileRefuse("settings_duplicate_selection", 400)
    available_servers = {server.id for server in catalog.mcp_servers}
    if not servers <= available_servers:
        raise ProfileRefuse("settings_server_unknown", 400)
    allowed_tools = {tool.id for server in catalog.mcp_servers if server.id in servers for tool in server.tools}
    if values.coder_enabled:
        if not catalog.coder.available:
            raise ProfileRefuse("settings_coder_unavailable", 400)
        allowed_tools.update(catalog.coder.tool_ids)
    if not tools <= allowed_tools:
        raise ProfileRefuse("settings_tool_unavailable_or_disabled", 400)


class ProfileSettings:
    def __init__(self, profiles: LocalProfiles) -> None:
        self.profiles = profiles

    def get(self, profile_id: str, revision: int | None = None) -> dict[str, object]:
        self.profiles.get(profile_id)
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ProfileRefuse("settings_revision_invalid", 400)
        if revision == 0:
            return {"revision": 0, "settings": None, "catalog_digest": None, "settings_digest": None}
        with self.profiles._connection(write=False) as connection:
            assert connection is not None
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='profile_settings'").fetchone()
            row = None
            if table is not None:
                if revision is None:
                    row = connection.execute(
                        "SELECT * FROM profile_settings WHERE profile_id=? ORDER BY revision DESC LIMIT 1",
                                             (profile_id,)).fetchone()
                else:
                    row = connection.execute("SELECT * FROM profile_settings WHERE profile_id=? AND revision=?",
                                             (profile_id, revision)).fetchone()
            if row is None:
                if revision is not None:
                    raise ProfileRefuse("settings_revision_not_found", 404)
                return {"revision": 0, "settings": None, "catalog_digest": None, "settings_digest": None}
            try:
                settings = SettingsValues.model_validate_json(row["settings_json"])
            except ValidationError as exc:
                raise ProfileRefuse("settings_store_invalid", 503) from exc
            if sha256_hex(canonical_json(settings.model_dump(mode="json"))) != row["settings_digest"]:
                raise ProfileRefuse("settings_store_digest_mismatch", 503)
            return {"revision": row["revision"], "settings": settings.model_dump(mode="json"),
                    "catalog_digest": row["catalog_digest"], "settings_digest": row["settings_digest"]}

    def save(self, profile_id: str, *, expected_revision: int, catalog_digest: str,
             settings: SettingsValues, catalog: SettingsCatalog) -> dict[str, object]:
        self.profiles.get(profile_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ProfileRefuse("settings_revision_invalid", 400)
        if catalog_digest != catalog.digest:
            raise ProfileRefuse("settings_catalog_changed", 409)
        validate_settings(settings, catalog)
        encoded = canonical_json(settings.model_dump(mode="json"))
        digest = sha256_hex(encoded)
        with self.profiles._connection(write=True) as connection:
            assert connection is not None
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT MAX(revision) FROM profile_settings WHERE profile_id=?",
                                         (profile_id,)).fetchone()[0]
            current_revision = 0 if current is None else current
            if current_revision != expected_revision:
                raise ProfileRefuse("settings_revision_conflict", 409)
            revision = current_revision + 1
            connection.execute("INSERT INTO profile_settings VALUES (?,?,?,?,?)",
                               (profile_id, revision, catalog_digest, encoded.decode("utf-8"), digest))
        return {"revision": revision, "settings": settings.model_dump(mode="json"),
                "catalog_digest": catalog_digest, "settings_digest": digest}
