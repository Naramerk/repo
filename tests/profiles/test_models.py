"""P2: frozen Profile / RoleConfig / RouteConfig / RoleName."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.core.execution.domain import Budget
from coscientist.mcp.config import StdioServerConfig, StreamableHttpServerConfig
from coscientist.profiles import RoleName
from coscientist.profiles.models import Identifier, Profile, RoleConfig, RouteConfig

_B = dict(max_llm_calls=4, max_tokens=8000, max_tool_calls=8)

def _budget() -> Budget:
    return Budget(**_B)

def _role(**o: object) -> RoleConfig:
    return RoleConfig.model_validate({"model": "m", "tools": ("search_pubmed",), **o})

def _route(**o: object) -> RouteConfig:
    return RouteConfig.model_validate({"role": "medical_agent", "budget": _budget(), **o})

def _pdata(**o: object) -> dict[str, object]:
    d: dict[str, object] = {
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
    d.update(o)
    return d

def _profile(**o: object) -> Profile:
    return Profile.model_validate(_pdata(**o))

def _err(exc: ValidationError) -> tuple[tuple[object, ...], str, str]:
    errs = exc.errors()
    assert len(errs) == 1
    return (tuple(errs[0]["loc"]), errs[0]["type"], errs[0]["msg"])

def test_role_name_exported() -> None:
    assert RoleName is Identifier
    with pytest.raises(ValidationError) as ei:
        _profile(roles={" medical_agent": _role()})
    assert _err(ei.value)[:2] == (("roles", " medical_agent", "[key]"), "value_error")

def test_json_round_trip() -> None:
    p = _profile(
        mcp_servers=(
            StdioServerConfig(
                transport="stdio", name="med", command="python", args=("-m", "x"), env_keys=()
            ),
        )
    )
    assert Profile.model_validate_json(p.model_dump_json()) == p

def test_profile_version_str_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(profile_version="1")
    assert _err(ei.value)[:2] == (("profile_version",), "int_type")

def test_profile_version_zero_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(profile_version=0)
    assert _err(ei.value)[:2] == (("profile_version",), "greater_than_equal")

def test_mcp_in_profile_dict_tuple_ok() -> None:
    srv = {
        "transport": "stdio",
        "name": "med",
        "command": "python",
        "args": ("-m", "x"),
        "env_keys": (),
    }
    assert _profile(mcp_servers=(srv,)).mcp_servers[0].args == ("-m", "x")

def test_mcp_http_api_key_env_int_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        StreamableHttpServerConfig.model_validate(
            {
                "transport": "streamable_http",
                "name": "remote",
                "url": "https://mcp.example/mcp",
                "api_key_env": 5,
            }
        )
    assert _err(ei.value)[:2] == (("api_key_env",), "string_type")

def test_mcp_http_url_whitespace_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        StreamableHttpServerConfig.model_validate(
            {
                "transport": "streamable_http",
                "name": "r",
                "url": "https://x/ mcp",
                "api_key_env": None,
            }
        )
    assert _err(ei.value)[:2] == (("url",), "value_error")

def test_mcp_duplicate_names_refused() -> None:
    a = StdioServerConfig(transport="stdio", name="dup", command="a", args=(), env_keys=())
    b = StdioServerConfig(transport="stdio", name="dup", command="b", args=(), env_keys=())
    with pytest.raises(ValidationError) as ei:
        _profile(mcp_servers=(a, b))
    assert _err(ei.value) == ((), "value_error", "Value error, mcp_servers names must be unique")

def test_role_tools_list_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        RoleConfig.model_validate({"model": "m", "tools": ["t"]})
    assert _err(ei.value)[:2] == (("tools",), "tuple_type")

def test_role_model_int_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        RoleConfig.model_validate({"model": 1, "tools": ()})
    assert _err(ei.value)[:2] == (("model",), "string_type")

def test_role_duplicate_tools_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        _role(tools=("search_pubmed", "search_pubmed"))
    assert _err(ei.value) == (
        (),
        "value_error",
        "Value error, RoleConfig.tools names must be unique",
    )

def test_empty_roles_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(roles={})
    assert _err(ei.value)[:2] == (("roles",), "too_short")

def test_empty_routes_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(routes={})
    assert _err(ei.value)[:2] == (("routes",), "too_short")

def test_whitespace_route_key_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(routes={" medical": _route()})
    assert _err(ei.value)[:2] == (("routes", " medical", "[key]"), "value_error")

def test_whitespace_integrations_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(integrations=(" ok",))
    assert _err(ei.value)[:2] == (("integrations", 0), "value_error")

def test_whitespace_plugins_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(plugins=("p", " bad"))
    assert _err(ei.value)[:2] == (("plugins", 1), "value_error")

def test_unknown_route_role_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(routes={"medical": _route(role="missing")})
    assert _err(ei.value) == (
        (),
        "value_error",
        "Value error, routes reference unknown roles: ['missing']",
    )

def test_route_missing_budget_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        RouteConfig.model_validate({"role": "medical_agent"})
    assert _err(ei.value)[:2] == (("budget",), "missing")

def test_profile_extra_forbid() -> None:
    with pytest.raises(ValidationError) as ei:
        Profile.model_validate({**_pdata(), "extra": 1})
    assert _err(ei.value)[:2] == (("extra",), "extra_forbidden")

def test_route_extra_forbid() -> None:
    with pytest.raises(ValidationError) as ei:
        RouteConfig.model_validate({"role": "r", "budget": _budget(), "x": 1})
    assert _err(ei.value)[:2] == (("x",), "extra_forbidden")

def test_profile_frozen() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile().name = "x"  # type: ignore[misc]
    assert _err(ei.value)[:2] == (("name",), "frozen_instance")

def test_rejects_bad_alembic() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(alembic="maybe")
    assert _err(ei.value)[:2] == (("alembic",), "literal_error")
