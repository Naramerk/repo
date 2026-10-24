"""MCP Stdio/HTTP server configs: strict + Identifier name."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from coscientist.mcp.config import Identifier, StdioServerConfig, StreamableHttpServerConfig

_IDENT = TypeAdapter(Identifier)


def _err(exc: ValidationError) -> tuple[tuple[object, ...], str]:
    errs = exc.errors()
    assert len(errs) == 1
    return (tuple(errs[0]["loc"]), errs[0]["type"])


def test_stdio_args_tuple_ok() -> None:
    s = StdioServerConfig.model_validate(
        {
            "transport": "stdio",
            "name": "med",
            "command": "python",
            "args": ("-m", "x"),
            "env_keys": (),
        }
    )
    assert s.args == ("-m", "x")


def test_stdio_args_list_refused() -> None:
    with pytest.raises(ValidationError) as ei:
        StdioServerConfig.model_validate(
            {
                "transport": "stdio",
                "name": "med",
                "command": "python",
                "args": ["-m", "x"],
                "env_keys": (),
            }
        )
    assert _err(ei.value) == (("args",), "tuple_type")


@pytest.mark.parametrize("bad", ["", " med", "med ", "me d"])
def test_identifier_empty_or_whitespace_refused(bad: str) -> None:
    with pytest.raises(ValidationError) as ei:
        _IDENT.validate_python(bad)
    assert _err(ei.value)[1] == "value_error"


def test_http_api_key_env_int_refused() -> None:
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


def test_http_url_whitespace_refused() -> None:
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
