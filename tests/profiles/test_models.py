"""P2: frozen Profile / RoleConfig / RouteConfig / RoleName."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.core.execution.domain import Budget
from coscientist.mcp.config import Identifier, StdioServerConfig, StreamableHttpServerConfig
from coscientist.profiles import RoleName
from coscientist.profiles.models import Profile, RoleConfig, RouteConfig


def _budget() -> Budget:
    return Budget(max_llm_calls=4, max_tokens=8000, max_tool_calls=8)


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


def _err(exc: ValidationError) -> tuple[tuple[object, ...], str]:
    errs = exc.errors()
    assert len(errs) == 1
    e = errs[0]
    return (tuple(e["loc"]), e["type"])


def test_role_name_exported() -> None:
    assert RoleName is Identifier
    with pytest.raises(ValidationError) as ei:
        _profile(roles={" medical_agent": _role()})
    assert _err(ei.value) == (("roles", " medical_agent", "[key]"), "value_error")


def test_json_round_trip() -> None:
    p = _profile(
        mcp_servers=(
            StdioServerConfig(
                transport="stdio", name="med", command="python", args=("-m", "x"), env_keys=()
            ),
            StreamableHttpServerConfig(
                transport="streamable_http",
                name="remote",
                url="https://mcp.example/mcp",
                api_key_env="KEY",
            ),
        )
    )
    assert Profile.model_validate_json(p.model_dump_json()) == p


def test_profile_version_str_and_zero() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(profile_version="1")
    assert _err(ei.value) == (("profile_version",), "int_type")
    with pytest.raises(ValidationError) as ei:
        _profile(profile_version=0)
    assert _err(ei.value) == (("profile_version",), "greater_than_equal")


def test_mcp_in_profile_dict_tuple_ok_list_fails() -> None:
    ok = {
        "transport": "stdio",
        "name": "med",
        "command": "python",
        "args": ("-m", "x"),
        "env_keys": (),
    }
    assert _profile(mcp_servers=(ok,)).mcp_servers[0].args == ("-m", "x")
    bad = {**ok, "args": ["-m", "x"]}
    with pytest.raises(ValidationError) as ei:
        _profile(mcp_servers=(bad,))
    assert _err(ei.value) == (("mcp_servers", 0, "stdio", "args"), "tuple_type")


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
    assert _err(ei.value) == (("api_key_env",), "string_type")


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
    assert _err(ei.value) == (("url",), "value_error")


def test_mcp_duplicate_names_refused() -> None:
    a = StdioServerConfig(transport="stdio", name="dup", command="a", args=(), env_keys=())
    b = StdioServerConfig(transport="stdio", name="dup", command="b", args=(), env_keys=())
    with pytest.raises(ValidationError) as ei:
        _profile(mcp_servers=(a, b))
    assert _err(ei.value) == ((), "value_error")
    assert "mcp_servers names must be unique" in str(ei.value)


def test_role_strict_tools_and_model() -> None:
    with pytest.raises(ValidationError) as ei:
        RoleConfig.model_validate({"model": "m", "tools": ["t"]})
    assert _err(ei.value) == (("tools",), "tuple_type")
    with pytest.raises(ValidationError) as ei:
        RoleConfig.model_validate({"model": 1, "tools": ()})
    assert _err(ei.value) == (("model",), "string_type")
    with pytest.raises(ValidationError) as ei:
        _role(tools=("search_pubmed", "search_pubmed"))
    assert _err(ei.value) == ((), "value_error")
    assert "tools names must be unique" in str(ei.value)


def test_empty_roles_and_routes() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(roles={})
    assert _err(ei.value) == (("roles",), "too_short")
    with pytest.raises(ValidationError) as ei:
        _profile(routes={})
    assert _err(ei.value) == (("routes",), "too_short")


def test_whitespace_keys_and_tuples() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(routes={" medical": _route()})
    assert _err(ei.value) == (("routes", " medical", "[key]"), "value_error")
    with pytest.raises(ValidationError) as ei:
        _profile(integrations=(" ok",))
    assert _err(ei.value) == (("integrations", 0), "value_error")
    with pytest.raises(ValidationError) as ei:
        _profile(plugins=("p", " bad"))
    assert _err(ei.value) == (("plugins", 1), "value_error")


def test_unknown_role_and_missing_budget() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(routes={"medical": _route(role="missing")})
    assert _err(ei.value) == ((), "value_error")
    assert "unknown roles" in str(ei.value)
    with pytest.raises(ValidationError) as ei:
        RouteConfig.model_validate({"role": "medical_agent"})
    assert _err(ei.value) == (("budget",), "missing")


def test_extra_forbid_and_frozen() -> None:
    with pytest.raises(ValidationError) as ei:
        Profile.model_validate({**_pdata(), "extra": 1})
    assert _err(ei.value) == (("extra",), "extra_forbidden")
    with pytest.raises(ValidationError) as ei:
        RouteConfig.model_validate({"role": "r", "budget": _budget(), "x": 1})
    assert _err(ei.value) == (("x",), "extra_forbidden")
    with pytest.raises(ValidationError) as ei:
        _profile().name = "x"  # type: ignore[misc]
    assert _err(ei.value) == (("name",), "frozen_instance")


def test_rejects_bad_alembic() -> None:
    with pytest.raises(ValidationError) as ei:
        _profile(alembic="maybe")
    assert _err(ei.value) == (("alembic",), "literal_error")
