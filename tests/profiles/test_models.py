"""G2: frozen Profile / RoleConfig / RouteConfig."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.profiles.models import Profile, RoleConfig, RouteBudget, RouteConfig


def _budget() -> RouteBudget:
    return RouteBudget(
        max_llm_calls=4, max_tokens=8000, max_tool_calls=8, timeout_s=120.0
    )


def _role() -> RoleConfig:
    return RoleConfig(model="m", prompt_ref="medical", tools=("search_pubmed",))


def _profile(**over: object) -> Profile:
    data: dict[str, object] = {
        "name": "medicine",
        "profile_version": "1.0.0",
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
    assert p.routes["medical"].role == "medical_agent"
    assert p.model_copy(update={"name": "x"}).name == "x"
    assert p.name == "medicine"
    with pytest.raises(ValidationError):
        p.name = "other"  # type: ignore[misc]


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
