"""Tests for frozen Profile domain models (P3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.profiles.models import (
    Profile,
    RoleConfig,
    RouteBudget,
    RouteConfig,
)


def _role(**overrides: object) -> RoleConfig:
    base: dict[str, object] = {
        "model": "test-model",
        "prompt_ref": "medical",
        "tools": ("search_pubmed",),
    }
    base.update(overrides)
    return RoleConfig.model_validate(base)


def _budget(**overrides: object) -> RouteBudget:
    base: dict[str, object] = {
        "max_llm_calls": 4,
        "max_tokens": 8000,
        "max_tool_calls": 8,
        "timeout_s": 120.0,
    }
    base.update(overrides)
    return RouteBudget.model_validate(base)


def _route(**overrides: object) -> RouteConfig:
    base: dict[str, object] = {"role": "medical_agent", "budget": _budget()}
    base.update(overrides)
    return RouteConfig.model_validate(base)


def _profile(**overrides: object) -> Profile:
    base: dict[str, object] = {
        "name": "medicine",
        "profile_version": "1.0.0",
        "roles": {"medical_agent": _role()},
        "routes": {"medical": _route()},
        "mcp_servers": (),
        "catalog_extensions": (),
        "alembic": "enabled",
        "integrations": (),
        "plugins": (),
    }
    base.update(overrides)
    return Profile.model_validate(base)


def test_profile_round_trip_frozen() -> None:
    p = _profile()
    assert p.name == "medicine"
    assert p.routes["medical"].role == "medical_agent"
    assert p.roles["medical_agent"].tools == ("search_pubmed",)
    copied = p.model_copy(update={"name": "medicine-fork"})
    assert copied.name == "medicine-fork"
    assert p.name == "medicine"
    with pytest.raises(ValidationError):
        p.name = "other"  # type: ignore[misc]


def test_profile_rejects_empty_roles() -> None:
    with pytest.raises(ValidationError):
        _profile(roles={})


def test_profile_rejects_route_unknown_role() -> None:
    with pytest.raises(ValidationError, match="unknown roles"):
        _profile(routes={"medical": _route(role="missing")})


def test_role_rejects_blank_tool_name() -> None:
    with pytest.raises(ValidationError):
        _role(tools=("ok", " "))


def test_route_budget_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _budget(timeout_s=0.0)


def test_alembic_must_be_enabled_or_disabled() -> None:
    with pytest.raises(ValidationError):
        _profile(alembic="maybe")


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        RoleConfig.model_validate(
            {
                "model": "m",
                "prompt_ref": "p",
                "tools": (),
                "surprise": True,
            }
        )


def test_profile_exports() -> None:
    from coscientist import profiles

    assert "Profile" in profiles.__all__
    assert profiles.Profile is Profile
