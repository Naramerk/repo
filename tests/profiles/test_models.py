"""P2: frozen Profile / RoleConfig / RouteConfig."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.core.execution.domain import Budget
from coscientist.mcp.config import StdioServerConfig, StreamableHttpServerConfig
from coscientist.profiles.models import Profile, RoleConfig, RouteConfig


def _budget() -> Budget:
    return Budget(max_llm_calls=4, max_tokens=8000, max_tool_calls=8)


def _role(**over: object) -> RoleConfig:
    data: dict[str, object] = {"model": "m", "tools": ("search_pubmed",)}
    data.update(over)
    return RoleConfig.model_validate(data)


def _route(**over: object) -> RouteConfig:
    data: dict[str, object] = {"role": "medical_agent", "budget": _budget()}
    data.update(over)
    return RouteConfig.model_validate(data)


def _profile(**over: object) -> Profile:
    data: dict[str, object] = {
        "name": "medicine",
        "profile_version": 1,
        "roles": {"medical_agent": _role()},
        "routes": {"medical": _route()},
        "mcp_servers": (),
        "catalog_extensions": (),
        "alembic": "enabled",
        "integrations": (),
        "plugins": (),
    }
    data.update(over)
    return Profile.model_validate(data)


def test_profile_json_round_trip() -> None:
    p = _profile(
        mcp_servers=(
            StdioServerConfig(
                transport="stdio",
                name="med",
                command="python",
                args=("-m", "x"),
                env_keys=(),
            ),
        )
    )
    restored = Profile.model_validate(p.model_dump(mode="python"))
    assert restored == p
    assert restored.mcp_servers[0].name == "med"
    assert restored.routes["medical"].budget.max_llm_calls == 4


def test_profile_version_refuses_str() -> None:
    with pytest.raises(ValidationError, match="profile_version"):
        _profile(profile_version="1")


def test_profile_version_refuses_zero() -> None:
    with pytest.raises(ValidationError, match="profile_version"):
        _profile(profile_version=0)


def test_mcp_stdio_refuses_list_args() -> None:
    with pytest.raises(ValidationError, match="args"):
        StdioServerConfig.model_validate(
            {
                "transport": "stdio",
                "name": "med",
                "command": "python",
                "args": ["-m", "x"],
                "env_keys": (),
            }
        )


def test_mcp_http_server_round_trip() -> None:
    srv = StreamableHttpServerConfig(
        transport="streamable_http",
        name="remote",
        url="https://mcp.example/mcp",
        api_key_env="KEY",
    )
    p = _profile(mcp_servers=(srv,))
    assert p.mcp_servers[0].url == "https://mcp.example/mcp"


def test_mcp_servers_duplicate_names_refused() -> None:
    a = StdioServerConfig(
        transport="stdio", name="dup", command="a", args=(), env_keys=()
    )
    b = StdioServerConfig(
        transport="stdio", name="dup", command="b", args=(), env_keys=()
    )
    with pytest.raises(ValidationError, match="mcp_servers names must be unique"):
        _profile(mcp_servers=(a, b))


def test_role_duplicate_tools_refused() -> None:
    with pytest.raises(ValidationError, match="tools names must be unique"):
        _role(tools=("search_pubmed", "search_pubmed"))


def test_rejects_empty_roles() -> None:
    with pytest.raises(ValidationError, match="non-empty mapping"):
        _profile(roles={})


def test_rejects_empty_routes() -> None:
    with pytest.raises(ValidationError, match="non-empty mapping"):
        _profile(routes={})


def test_rejects_whitespace_role_key() -> None:
    with pytest.raises(ValidationError, match="whitespace"):
        _profile(roles={" medical_agent": _role()})


def test_rejects_whitespace_catalog_extension() -> None:
    with pytest.raises(ValidationError, match="whitespace"):
        _profile(catalog_extensions=("ok", " bad"))


def test_rejects_unknown_route_role() -> None:
    with pytest.raises(ValidationError, match="unknown roles"):
        _profile(routes={"medical": _route(role="missing")})


def test_route_requires_budget() -> None:
    with pytest.raises(ValidationError, match="budget"):
        RouteConfig.model_validate({"role": "medical_agent"})


def test_rejects_blank_tool() -> None:
    with pytest.raises(ValidationError, match="whitespace"):
        _role(tools=(" ",))


def test_rejects_bad_alembic() -> None:
    with pytest.raises(ValidationError, match="alembic"):
        _profile(alembic="maybe")


def test_rejects_role_extra_field() -> None:
    with pytest.raises(ValidationError, match="extra"):
        RoleConfig.model_validate(
            {"model": "m", "tools": (), "prompt_ref": "nope"}
        )
