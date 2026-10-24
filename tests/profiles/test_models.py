"""G2: frozen Profile / RoleConfig / RouteConfig."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.profiles.models import (
    Profile,
    ProfileStdioServerConfig,
    RoleConfig,
    RouteBudget,
    RouteConfig,
)


def _budget(**over: object) -> RouteBudget:
    data: dict[str, object] = {
        "max_llm_calls": 4,
        "max_tokens": 8000,
        "max_tool_calls": 8,
        "timeout_s": 120.0,
    }
    data.update(over)
    return RouteBudget.model_validate(data)


def _role() -> RoleConfig:
    return RoleConfig(model="m", prompt_ref="medical", tools=("search_pubmed",))


def _profile(**over: object) -> Profile:
    data: dict[str, object] = {
        "name": "medicine",
        "profile_version": 1,
        "roles": {"medical_agent": _role()},
        "routes": {"medical": RouteConfig(role="medical_agent", budget=_budget())},
        "mcp_servers": (),
        "catalog_extensions": (),
        "alembic": "enabled",
        "integrations": (),
        "plugins": (),
    }
    data.update(over)
    return Profile.model_validate(data)


def test_profile_frozen_round_trip() -> None:
    p = _profile()
    assert p.profile_version == 1
    assert p.routes["medical"].role == "medical_agent"
    assert p.model_copy(update={"name": "x"}).name == "x"
    assert p.name == "medicine"
    with pytest.raises(ValidationError):
        p.name = "other"  # type: ignore[misc]


def test_profile_version_refuses_non_int() -> None:
    with pytest.raises(ValidationError):
        _profile(profile_version="1")
    with pytest.raises(ValidationError):
        _profile(profile_version="1.0.0")
    with pytest.raises(ValidationError):
        _profile(profile_version=1.0)
    with pytest.raises(ValidationError):
        _profile(profile_version=0)
    with pytest.raises(ValidationError):
        _profile(profile_version=True)


def test_route_budget_refuses_str_numeric() -> None:
    """Strict model: string '4' must not coerce into an int field."""
    with pytest.raises(ValidationError):
        _budget(max_llm_calls="4")


def test_mcp_servers_strict_refuses_list_args_and_str_coercion() -> None:
    """Profile MCP entries are strict — list↛tuple; no silent coerce."""
    with pytest.raises(ValidationError):
        ProfileStdioServerConfig.model_validate(
            {
                "transport": "stdio",
                "name": "med",
                "command": "python",
                "args": ["-m", "x"],  # list, not tuple
                "env_keys": (),
            }
        )
    with pytest.raises(ValidationError):
        _profile(
            mcp_servers=[
                {
                    "transport": "stdio",
                    "name": "med",
                    "command": "python",
                    "args": ["-m", "x"],
                    "env_keys": (),
                }
            ]
        )
    ok = ProfileStdioServerConfig(
        transport="stdio",
        name="med",
        command="python",
        args=("-m", "x"),
        env_keys=(),
    )
    p = _profile(mcp_servers=(ok,))
    assert p.mcp_servers[0].name == "med"


def test_rejects_empty_roles() -> None:
    with pytest.raises(ValidationError):
        _profile(roles={})


def test_rejects_unknown_route_role() -> None:
    with pytest.raises(ValidationError, match="unknown roles"):
        _profile(
            routes={"medical": RouteConfig(role="missing", budget=_budget())}
        )


def test_rejects_blank_tool_and_bad_alembic() -> None:
    with pytest.raises(ValidationError):
        RoleConfig(model="m", prompt_ref="p", tools=(" ",))
    with pytest.raises(ValidationError):
        _profile(alembic="maybe")


def test_extra_forbidden_and_exports() -> None:
    with pytest.raises(ValidationError):
        RoleConfig.model_validate(
            {"model": "m", "prompt_ref": "p", "tools": (), "x": 1}
        )
    import coscientist.profiles as profiles

    assert profiles.Profile is Profile
    assert "RouteConfig" in profiles.__all__
    assert "ProfileStdioServerConfig" in profiles.__all__
